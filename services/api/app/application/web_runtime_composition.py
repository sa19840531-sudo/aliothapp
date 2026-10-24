"""Web V2 探索与并行正式质检通知的生产组合根。"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from services.api.app.agents.explorer.web_assessment_agent import (
    WebExplorationAssessmentAgent,
)
from services.api.app.agents.runtime.action_trace import InMemoryActionTraceSink
from services.api.app.agents.runtime.models import (
    ErrorCategory,
    ExecutionOutcome,
    RunResult,
    TerminationReason,
)
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.application.inspection_composition import (
    build_default_web_page_inspection_notification,
)
from services.api.app.application.page_type_judgements import PageTypeJudgementLedger
from services.api.app.application.web_ad_landing_evidence import (
    WebAdLandingEvidenceScheduler,
    schedule_ready_web_business_page_ad_evidence,
)
from services.api.app.application.web_age_gate import WebAgeGateHandler
from services.api.app.application.web_assessment_evidence import (
    VerifiedWebAssessmentEvidenceLoader,
)
from services.api.app.application.web_auxiliary_navigation import (
    WebAuxiliaryNavigationExplorer,
)
from services.api.app.application.web_browser_profile import WebBrowserProfileRegistry
from services.api.app.application.web_desktop_exploration import (
    WebDesktopExplorationSummary,
    WebDesktopMenuPageVisitor,
)
from services.api.app.application.web_desktop_navigation import catalog_navigation_url_matches
from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerConflictError,
    WebExplorationBudgetLedgerPort,
    WebMenuLedgerTerminalRecord,
)
from services.api.app.application.web_exploration_workflow import (
    WEB_SURFACE_SESSION_OWNER,
    RunWebWorkflowCommand,
    WebSurfaceBootstrapper,
)
from services.api.app.application.web_formal_session_closure import (
    DurableWebFormalSessionCloser,
)
from services.api.app.application.web_frontier_scheduler import (
    TrustedWebFrontierCandidateSource,
)
from services.api.app.application.web_full_page_supplemental_capture import (
    WebFullPageSupplementalCaptureService,
    WebFullPageSupplementingReadyCaptureReader,
)
from services.api.app.application.web_guide_dispatcher import WebGuideBranchDispatcher
from services.api.app.application.web_guide_page import WebGuideBranch
from services.api.app.application.web_interaction_precision import assess_web_interaction_outcome
from services.api.app.application.web_list_detail_navigation import (
    WebListDetailNavigator,
    WebListDetailResult,
)
from services.api.app.application.web_menu_exploration import (
    WebMenuExplorer,
    WebMenuVisitResult,
    hydrate_web_menu_summary,
    restore_persisted_navigation_state,
)
from services.api.app.application.web_page_classification import WebPageClassifier
from services.api.app.application.web_page_inspection_notification import (
    WebInspectionPublicationWakeup,
    WebPageInspectionNotifyingClassifier,
)
from services.api.app.application.web_page_readiness import (
    WebPageReadinessCoordinator,
    web_readiness_result_is_ready,
)
from services.api.app.application.web_page_type_assessment import (
    WebAgeGatePageTypeRecorder,
    WebPageTypeAssessmentRecorder,
    WebPageTypeAssessmentRecordingClassifier,
    WebPageTypeCatalogIdentity,
)
from services.api.app.application.web_player_visual_recheck import (
    WebPlayerVisualRecheckService,
)
from services.api.app.application.web_primary_content_capture import (
    WebPrimaryContentCaptureService,
)
from services.api.app.application.web_primary_content_diagnostic import (
    primary_content_diagnostic,
    primary_content_failure,
)
from services.api.app.application.web_product_capability_assessment import (
    WebProductCapabilityAssessment,
    assess_web_product_capabilities,
)
from services.api.app.application.web_run_policy import (
    build_new_web_navigation_context,
    load_published_web_policy,
    materialize_web_policy,
)
from services.api.app.application.web_runtime_evidence import (
    WebAgeEvidenceReaderAdapter,
    WebFrontierCaptureMetadataReaderAdapter,
    WebPageEvidenceReaderAdapter,
    WebReadyCaptureReaderAdapter,
    WebRuntimeEvidenceSource,
)
from services.api.app.application.web_workflow_dispatch import (
    RuntimeBackedWebWorkflowHandler,
)
from services.api.app.core.config import Settings
from services.api.app.domain.exploration import (
    WEB_DETAIL_PAGE_TYPES,
    WEB_LIST_PAGE_TYPES,
    WebMenuTerminalStatus,
)
from services.api.app.domain.exploration.web_run_policy_snapshot import (
    WebRunPolicyError,
    WebRunPolicySnapshot,
)
from services.api.app.domain.page import (
    WebMenuDecision,
    WebSiteMenuCatalog,
)
from services.api.app.domain.page.web_runtime_navigation_context import RuntimeWebNavigationContext
from services.api.app.domain.task import AdultSelfAttestation
from services.api.app.infrastructure.browser.capture_runtime import (
    WebCaptureRuntime,
    build_default_web_capture_runtime,
)
from services.api.app.infrastructure.db.page_type_judgement_repository import (
    PostgresPageTypeJudgementRepository,
)
from services.api.app.infrastructure.db.surface_phase_repository import (
    PostgresSurfacePhaseRepository,
)
from services.api.app.infrastructure.db.web_assessment_evidence_repository import (
    PostgresWebAssessmentArtifactMetadataReader,
)
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    PostgresWebExplorationBudgetLedgerRepository,
    require_budget_policy_match,
)
from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
    PostgresWebAuxiliaryNavigationClaimRepository,
    PostgresWebFullPageSupplementalClaimRepository,
)
from services.api.app.infrastructure.db.web_guide_evidence import (
    PostgresWebGuideEvidenceMarker,
    WebGuideEvidenceWriter,
)
from services.api.app.infrastructure.db.web_guide_phase_ledger import WebGuidePhaseLedger
from services.api.app.infrastructure.db.web_popup_blocked_repository import (
    PostgresWebPopupBlockedRepository,
    WebPopupBlockedPersistenceError,
)
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    PostgresWebRunPolicySnapshotRepository,
)
from services.api.app.infrastructure.llm_gateway.client_factory import build_chat_client
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord
from services.api.app.infrastructure.storage.factory import (
    build_evidence_artifact_store,
)


class WebRuntimeConfigurationError(RuntimeError):
    """持久化 Web Run 不满足独立 Runtime 的冻结合同。"""


@dataclass(frozen=True, kw_only=True)
class WebHomePreparationResult:
    coordination: object
    ready_capture_id: str
    age_gate_result: object | None = None
    readiness_result: object | None = None
    page_type: str | None = None
    raw_page_type: str | None = None
    product_capability_assessment: WebProductCapabilityAssessment | None = None
    primary_content_diagnostics: tuple[dict[str, object], ...] = ()
    navigation_current_capture_id: str | None = None
    navigation_handoff_error: str | None = None
    ad_landing_evidence: Mapping[str, object] = field(default_factory=dict)


class WebOrchestratedMenuPageVisitor:
    """把菜单导航、就绪门、分类和一层详情组合为一个账本访问结果。"""

    def __init__(
        self,
        *,
        navigator: WebDesktopMenuPageVisitor,
        readiness: WebPageReadinessCoordinator,
        ready_capture_reader: WebReadyCaptureReaderAdapter,
        classifier: WebPageClassifier,
        evidence_source: WebRuntimeEvidenceSource,
        detail_navigator: WebListDetailNavigator,
        primary_content_capture: WebPrimaryContentCaptureService | None = None,
        player_visual_recheck: WebPlayerVisualRecheckService | None = None,
        navigation_candidate_source: object | None = None,
        ad_landing_scheduler: WebAdLandingEvidenceScheduler | None = None,
        ad_identity_policy: object | None = None,
        ad_registered_domain: str | None = None,
    ) -> None:
        self._navigator = navigator
        self._readiness = readiness
        self._ready_capture_reader = ready_capture_reader
        self._classifier = classifier
        self._evidence_source = evidence_source
        self._detail_navigator = detail_navigator
        self._primary_content_capture = primary_content_capture
        self._player_visual_recheck = player_visual_recheck
        self._navigation_candidate_source = navigation_candidate_source
        self._ad_landing_scheduler = ad_landing_scheduler
        self._ad_identity_policy = ad_identity_policy
        self._ad_registered_domain = ad_registered_domain

    async def visit(self, **kwargs: object) -> WebMenuVisitResult:
        result = await self._visit(kwargs)
        if result.navigation_current_capture_id:
            # 非就绪菜单页仍可把 readiness 的最后一帧交给下一次冻结 href GOTO；
            # 下一菜单领取会在数据库事务内再次证明它确为 Session current。
            return result
        if (
            self._primary_content_capture is None
            or result.status is WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED
        ):
            return result
        try:
            current_id = await _read_navigation_handoff(
                self._ready_capture_reader,
                self._navigation_candidate_source,
                session_id=str(getattr(kwargs.get("command"), "session_id", "") or ""),
                page_role="menu",
            )
            return replace(result, navigation_current_capture_id=current_id)
        except Exception:  # noqa: BLE001 - 保留已发生动作/分类，后续菜单必须明确阻断
            recovery = _failed_detail_recovery_source(result)
            if recovery is not None:
                recovery_source_id, recovery_page_role, requires_rebaseline = recovery
                try:
                    session_id = str(getattr(kwargs.get("command"), "session_id", "") or "")
                    if requires_rebaseline:
                        readiness = await self._readiness.prepare(
                            source_capture_id=recovery_source_id,
                            page_role=recovery_page_role,
                        )
                        if not web_readiness_result_is_ready(readiness):
                            raise RuntimeError("WEB_NAVIGATION_RECOVERY_NOT_READY")
                        current_id = await _read_navigation_handoff(
                            self._ready_capture_reader,
                            self._navigation_candidate_source,
                            session_id=session_id,
                            page_role=recovery_page_role,
                        )
                    else:
                        _bind_navigation_capture(
                            self._navigation_candidate_source,
                            session_id=session_id,
                            capture_id=recovery_source_id,
                        )
                        current_id = recovery_source_id
                    return replace(
                        result,
                        navigation_current_capture_id=current_id,
                    )
                except Exception:  # noqa: BLE001 - 恢复失败继续使用固定交接错误
                    pass
            return replace(result, navigation_handoff_error="WEB_NAVIGATION_CURRENT_UNTRUSTED")

    async def _visit(self, kwargs: dict[str, object]) -> WebMenuVisitResult:
        command = kwargs.get("command")
        entry = kwargs.get("entry")
        session_id = str(getattr(command, "session_id", "") or "")
        probe = await self._navigator.visit(**kwargs)
        menu_lineage = {
            "menu_action_count": probe.menu_action_count,
            "menu_intent_id": probe.menu_intent_id,
            "menu_action_execution_id": probe.menu_action_execution_id,
            "menu_interaction_outcome": probe.menu_interaction_outcome,
            "menu_interaction_plan": probe.menu_interaction_plan,
            "menu_before_url": probe.menu_before_url,
            "menu_before_content_identity": probe.menu_before_content_identity,
            "menu_receipt_verified": probe.menu_receipt_verified,
        }
        primary_diagnostics = []

        def record_primary(result):
            diagnostic = primary_content_diagnostic(result)
            if diagnostic is not None:
                primary_diagnostics.append(diagnostic)
                menu_lineage["primary_content_diagnostics"] = tuple(primary_diagnostics)
            return result

        def post_menu_failed(
            *,
            final_capture_id: str | None,
            error_code: str,
            ready_capture_id: str | None = None,
            exc: Exception,
            detail: WebListDetailResult | None = None,
            page_type: str | None = None,
            menu_page_type: str | None = None,
            menu_readiness_evidence_capture_ids: tuple[str, ...] = (),
            menu_dismissed_layers: tuple[str, ...] = (),
        ) -> WebMenuVisitResult:
            return WebMenuVisitResult(
                final_capture_id=final_capture_id,
                page_type=page_type,
                status=WebMenuTerminalStatus.NAVIGATION_FAILED,
                error_code=error_code,
                detail_capture_id=(detail.detail_capture_id if detail else None),
                menu_probe_capture_id=probe.final_capture_id,
                menu_ready_capture_id=ready_capture_id,
                menu_page_type=menu_page_type,
                menu_readiness_evidence_capture_ids=(menu_readiness_evidence_capture_ids),
                menu_dismissed_layers=menu_dismissed_layers,
                detail_candidate_ids=(detail.detail_candidate_ids if detail else ()),
                selected_detail_candidate_id=(detail.selected_candidate_id if detail else None),
                detail_selection_confidence=(
                    detail.detail_selection_confidence if detail else None
                ),
                detail_selection_reason=(detail.detail_selection_reason if detail else None),
                detail_selection_diagnostic=(
                    detail.detail_selection_diagnostic if detail else None
                ),
                detail_probe_capture_id=(detail.probe_capture_id if detail else None),
                detail_ready_capture_id=(detail.detail_capture_id if detail else None),
                detail_page_type=(detail.page_type if detail else None),
                detail_readiness_evidence_capture_ids=(
                    detail.readiness_evidence_capture_ids if detail else ()
                ),
                detail_dismissed_layers=(detail.dismissed_layers if detail else ()),
                detail_action_count=(detail.detail_action_count if detail else 0),
                detail_intent_id=(detail.detail_intent_id if detail else None),
                detail_action_execution_id=(detail.detail_action_execution_id if detail else None),
                detail_interaction_outcome=(detail.interaction_outcome if detail else None),
                detail_scroll_interaction_outcomes=(
                    detail.scroll_interaction_outcomes if detail else ()
                ),
                detail_dismissal_interaction_outcomes=(
                    detail.readiness_interaction_outcomes if detail else ()
                ),
                diagnostic_type=_exception_type(exc),
                diagnostic_message="菜单动作后处理失败，已保留实际动作血缘",
                **menu_lineage,
            )

        if probe.final_capture_id:
            try:
                _bind_navigation_capture(
                    self._navigation_candidate_source,
                    session_id=session_id,
                    capture_id=probe.final_capture_id,
                )
            except Exception as exc:  # noqa: BLE001 - 菜单动作血缘不得因绑定失败丢失
                return post_menu_failed(
                    final_capture_id=probe.final_capture_id,
                    error_code="WEB_MENU_POST_NAVIGATION_FAILED",
                    exc=exc,
                )
        if not probe.final_capture_id:
            return WebMenuVisitResult(
                final_capture_id=None,
                page_type=None,
                status=WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED,
                error_code="WEB_MENU_PROBE_MISSING",
                **menu_lineage,
            )
        from services.api.app.application.web_submenu_expansion import is_proven_submenu_expansion

        if is_proven_submenu_expansion(probe):
            # 展开证明只推进菜单树，不把同一页面签发成新的业务页面。
            return probe
        if probe.status is not WebMenuTerminalStatus.MENU_PAGE_CAPTURED:
            return WebMenuVisitResult(
                final_capture_id=probe.final_capture_id,
                page_type=None,
                status=probe.status,
                error_code=probe.error_code,
                menu_probe_capture_id=probe.final_capture_id,
                **menu_lineage,
            )
        try:
            readiness = await self._readiness.prepare(
                source_capture_id=probe.final_capture_id,
                page_role="menu",
            )
        except Exception as exc:  # noqa: BLE001 - 菜单动作已发生，保留实际动作血缘
            return post_menu_failed(
                final_capture_id=probe.final_capture_id,
                error_code="WEB_MENU_POST_NAVIGATION_FAILED",
                exc=exc,
            )
        menu_lineage["menu_dismissal_interaction_outcomes"] = getattr(
            readiness, "interaction_outcomes", ()
        )
        if not web_readiness_result_is_ready(readiness):
            navigation_current_capture_id = None
            if readiness.status != "human_verification_blocked":
                evidence_capture_ids = tuple(readiness.evidence_capture_ids)
                if evidence_capture_ids and isinstance(evidence_capture_ids[-1], str):
                    navigation_current_capture_id = evidence_capture_ids[-1]
            return WebMenuVisitResult(
                final_capture_id=probe.final_capture_id,
                page_type=None,
                status=(
                    WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED
                    if readiness.status == "human_verification_blocked"
                    else WebMenuTerminalStatus.POPUP_BLOCKED
                ),
                error_code=(
                    "WEB_MENU_HUMAN_VERIFICATION_BLOCKED"
                    if readiness.status == "human_verification_blocked"
                    else "WEB_MENU_POPUP_BLOCKED"
                ),
                menu_probe_capture_id=probe.final_capture_id,
                menu_readiness_evidence_capture_ids=(readiness.evidence_capture_ids),
                menu_dismissed_layers=readiness.dismissed_layers,
                navigation_current_capture_id=navigation_current_capture_id,
                **menu_lineage,
            )
        try:
            _bind_navigation_capture(
                self._navigation_candidate_source,
                session_id=session_id,
                capture_id=readiness.ready_capture_id,
            )
        except Exception as exc:  # noqa: BLE001 - 菜单动作血缘不得因绑定失败丢失
            return post_menu_failed(
                final_capture_id=readiness.ready_capture_id,
                ready_capture_id=readiness.ready_capture_id,
                error_code="WEB_MENU_POST_NAVIGATION_FAILED",
                exc=exc,
                menu_readiness_evidence_capture_ids=readiness.evidence_capture_ids,
                menu_dismissed_layers=readiness.dismissed_layers,
            )
        try:
            ready_capture = await self._ready_capture_reader.read(
                capture_id=readiness.ready_capture_id,
                page_role="menu",
            )
            from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry

            if (
                isinstance(entry, WebRuntimeMenuEntry)
                and entry.canonical_url is None
                and catalog_navigation_url_matches(
                    probe.menu_before_url, getattr(ready_capture, "url", "")
                )
            ):
                return post_menu_failed(
                    final_capture_id=readiness.ready_capture_id,
                    ready_capture_id=readiness.ready_capture_id,
                    error_code="WEB_DISCOVERY_SAME_URL_OBJECT_UNPROVEN",
                    exc=RuntimeError("目录外无 href 状态无法证明独立对象"),
                )
            if is_dataclass(ready_capture):
                ready_capture = replace(
                    ready_capture,
                    classification_hint=getattr(entry, "classification_hint", None),
                    menu_entry_ids=(str(getattr(entry, "entry_id", "")),),
                )
            page = await self._classifier.classify(ready_capture)
        except Exception as exc:  # noqa: BLE001 - 菜单动作已发生，保留实际动作血缘
            return post_menu_failed(
                final_capture_id=readiness.ready_capture_id,
                ready_capture_id=readiness.ready_capture_id,
                error_code="WEB_MENU_POST_NAVIGATION_FAILED",
                exc=exc,
            )
        if (
            self._ad_landing_scheduler is not None
            and self._ad_identity_policy is not None
            and self._ad_registered_domain is not None
        ):
            try:
                ad_result = await schedule_ready_web_business_page_ad_evidence(
                    scheduler=self._ad_landing_scheduler,
                    snapshot_reader=self._evidence_source,
                    identity_policy=self._ad_identity_policy,
                    registered_domain=self._ad_registered_domain,
                    task_id=str(getattr(command, "task_id", "") or ""),
                    agent_run_id=str(getattr(command, "agent_run_id", "") or ""),
                    session_id=session_id,
                    page_role="menu",
                    page_type=getattr(page, "page_type", None),
                    ready_capture=ready_capture,
                    evidence_capture_ids=readiness.evidence_capture_ids,
                )
                menu_lineage["ad_landing_evidence"] = ad_result.to_dict()
            except Exception:  # noqa: BLE001 - 广告附件失败不伪装成功，也不抹除业务页终态
                menu_lineage["ad_landing_evidence"] = {
                    "status": "scheduling_failed",
                    "fail_closed": True,
                    "reason_code": "WEB_AD_EVIDENCE_SCHEDULING_FAILED",
                }
        primary_result = None
        facts = getattr(ready_capture, "interaction_facts", None)
        if probe.menu_interaction_plan is not None:
            expected_url = str(
                getattr(entry, "canonical_url", "") or probe.menu_observed_target_url or ""
            )
            target_arrived = bool(
                facts
                and expected_url
                and (
                    (
                        catalog_navigation_url_matches(expected_url, facts.url)
                        and not catalog_navigation_url_matches(probe.menu_before_url, facts.url)
                    )
                    or expected_url in facts.subject_urls
                )
            )
            matches_target = getattr(self._evidence_source, "matches_target", None)
            if not target_arrived and callable(matches_target):
                target_arrived = await matches_target(
                    capture_id=readiness.ready_capture_id, expected_url=expected_url
                )
            outcome = assess_web_interaction_outcome(
                plan=probe.menu_interaction_plan,
                command_accepted=bool(
                    probe.menu_interaction_outcome
                    and probe.menu_interaction_outcome.command_accepted
                ),
                before_url=probe.menu_before_url,
                after_url=facts.url if facts else probe.menu_before_url,
                before_content_identity=probe.menu_before_content_identity,
                after_content_identity=facts.content_identity if facts else None,
                postcondition_satisfied=target_arrived,
                after_capture_id=readiness.ready_capture_id,
                readiness_accepted=bool(facts and facts.readiness_accepted),
                identity_verified=bool(
                    facts
                    and facts.identity_verified
                    and target_arrived
                    and probe.menu_receipt_verified
                ),
                business_layer_visible=bool(facts and facts.business_layer_visible),
                **({"evidence_mode": facts.evidence_mode} if facts else {}),
            )
            menu_lineage["menu_interaction_outcome"] = outcome
            if not outcome.transition_arrived:
                return post_menu_failed(
                    final_capture_id=readiness.ready_capture_id,
                    ready_capture_id=readiness.ready_capture_id,
                    error_code="WEB_MENU_POSTCONDITION_NOT_SATISFIED",
                    exc=RuntimeError(outcome.failure_reason),
                    menu_readiness_evidence_capture_ids=readiness.evidence_capture_ids,
                    menu_dismissed_layers=readiness.dismissed_layers,
                )
        if getattr(page, "reused_capture_id", None):
            return WebMenuVisitResult(
                final_capture_id=readiness.ready_capture_id,
                page_type=page.page_type,
                status=WebMenuTerminalStatus.LOGICAL_TARGET_REUSED,
                menu_probe_capture_id=probe.final_capture_id,
                menu_ready_capture_id=readiness.ready_capture_id,
                menu_page_type=page.page_type,
                reused_capture_id=page.reused_capture_id,
                reused_logical_page_id=page.reused_logical_page_id,
                **menu_lineage,
            )
        classification_capture_id = readiness.ready_capture_id
        if page.page_type in {None, "unknown"}:
            page, primary_result, supplement_capture_id = await self._classify_primary_content_once(
                command=command,
                entry=entry,
                ready_capture=ready_capture,
                initial_assessment=page,
            )
            classification_capture_id = supplement_capture_id or classification_capture_id
            record_primary(primary_result)
        if page.page_type in {None, "unknown"}:
            return WebMenuVisitResult(
                final_capture_id=readiness.ready_capture_id,
                page_type=page.page_type,
                status=WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED,
                error_code="WEB_PAGE_TYPE_UNCLASSIFIED",
                menu_probe_capture_id=probe.final_capture_id,
                menu_ready_capture_id=classification_capture_id,
                menu_page_type=(
                    getattr(page, "proposed_page_type", None)
                    or getattr(page, "raw_page_type", None)
                ),
                menu_readiness_evidence_capture_ids=(readiness.evidence_capture_ids),
                menu_dismissed_layers=readiness.dismissed_layers,
                diagnostic_type=getattr(page, "needs_review_reason", None),
                diagnostic_message=getattr(page, "diagnostic_message", None),
                **menu_lineage,
            )
        if page.page_type in WEB_DETAIL_PAGE_TYPES:
            if primary_result is None:
                primary_result = record_primary(
                    await self._capture_primary_content(
                        command=command,
                        ready_capture=ready_capture,
                        page_type=page.page_type,
                    )
                )
            return WebMenuVisitResult(
                final_capture_id=readiness.ready_capture_id,
                page_type=page.page_type,
                status=WebMenuTerminalStatus.DETAIL_CAPTURED,
                detail_capture_id=readiness.ready_capture_id,
                menu_probe_capture_id=probe.final_capture_id,
                menu_ready_capture_id=classification_capture_id,
                menu_page_type=getattr(page, "raw_page_type", page.page_type),
                menu_readiness_evidence_capture_ids=(readiness.evidence_capture_ids),
                menu_dismissed_layers=readiness.dismissed_layers,
                detail_ready_capture_id=classification_capture_id,
                detail_page_type=page.page_type,
                detail_action_count=0,
                **menu_lineage,
            )
        if page.page_type in {"search_page", "activity"}:
            # 正式分类已经由 recorder 保存；既有菜单账本列仍是五类约束，
            # 新辅助页型只能以不匹配终态关闭，不能塞入 menu_page_type。
            return WebMenuVisitResult(
                final_capture_id=readiness.ready_capture_id,
                page_type=page.page_type,
                status=WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED,
                error_code="WEB_MENU_PAGE_TYPE_MISMATCH",
                menu_probe_capture_id=probe.final_capture_id,
                menu_ready_capture_id=classification_capture_id,
                menu_page_type=None,
                menu_readiness_evidence_capture_ids=(readiness.evidence_capture_ids),
                menu_dismissed_layers=readiness.dismissed_layers,
                diagnostic_type="page_type_mismatch",
                diagnostic_message="菜单落地页已正式判定为 Web 辅助业务类型",
                **menu_lineage,
            )
        if page.page_type not in WEB_LIST_PAGE_TYPES:
            return WebMenuVisitResult(
                final_capture_id=readiness.ready_capture_id,
                page_type=page.page_type,
                menu_probe_capture_id=probe.final_capture_id,
                menu_ready_capture_id=classification_capture_id,
                menu_page_type=getattr(page, "raw_page_type", page.page_type),
                menu_readiness_evidence_capture_ids=(readiness.evidence_capture_ids),
                menu_dismissed_layers=readiness.dismissed_layers,
                **menu_lineage,
            )
        try:
            list_source_capture_id = readiness.ready_capture_id
            if primary_result is None:
                primary_result = record_primary(
                    await self._capture_primary_content(
                        command=command,
                        ready_capture=ready_capture,
                        page_type=page.page_type,
                    )
                )
            if self._primary_content_capture is not None:
                list_source_capture_id = await _read_navigation_handoff(
                    self._ready_capture_reader,
                    self._navigation_candidate_source,
                    session_id=session_id,
                    page_role="menu",
                )
            detail = await self._detail_navigator.capture_one(
                await self._evidence_source.read_list(
                    capture_id=list_source_capture_id,
                ),
                list_page_type=page.page_type,
                command=command,
                entry=entry,
            )
        except Exception as exc:  # noqa: BLE001 - 菜单动作已发生，保留实际动作血缘
            return post_menu_failed(
                final_capture_id=readiness.ready_capture_id,
                ready_capture_id=readiness.ready_capture_id,
                error_code="WEB_MENU_POST_NAVIGATION_FAILED",
                exc=exc,
                page_type=page.page_type,
                menu_page_type=getattr(page, "raw_page_type", page.page_type),
                menu_readiness_evidence_capture_ids=readiness.evidence_capture_ids,
                menu_dismissed_layers=readiness.dismissed_layers,
            )
        if detail.detail_capture_id:
            try:
                _bind_navigation_capture(
                    self._navigation_candidate_source,
                    session_id=session_id,
                    capture_id=detail.detail_capture_id,
                )
            except Exception as exc:  # noqa: BLE001 - 已有菜单/详情血缘必须进入失败终态
                return post_menu_failed(
                    final_capture_id=detail.detail_capture_id,
                    ready_capture_id=readiness.ready_capture_id,
                    error_code="WEB_MENU_POST_NAVIGATION_FAILED",
                    exc=exc,
                    detail=detail,
                    page_type=page.page_type,
                    menu_page_type=getattr(page, "raw_page_type", page.page_type),
                    menu_readiness_evidence_capture_ids=(readiness.evidence_capture_ids),
                    menu_dismissed_layers=readiness.dismissed_layers,
                )
            if detail.page_type in WEB_DETAIL_PAGE_TYPES and not detail.reused_capture_id:
                visual_capture_id = detail.detail_capture_id
                detail_primary = None
                try:
                    detail_ready_capture = await self._ready_capture_reader.read(
                        capture_id=detail.detail_capture_id,
                        page_role="detail",
                    )
                    detail_primary = await self._capture_primary_content(
                        command=command,
                        ready_capture=detail_ready_capture,
                        page_type=detail.page_type,
                    )
                    record_primary(detail_primary)
                    if getattr(detail_primary, "evidence_capture_id", None):
                        visual_capture_id = detail_primary.evidence_capture_id
                except Exception as exc:  # noqa: BLE001 - 详情基线保留，失败事实进入终态报告
                    record_primary(primary_content_failure(detail.detail_capture_id, exc))
                if (
                    self._player_visual_recheck is not None
                    and detail.page_type in {"content_detail", "video_detail"}
                ):
                    try:
                        visual_ready = await self._ready_capture_reader.read(
                            capture_id=visual_capture_id, page_role="detail"
                        )
                        await self._player_visual_recheck.assess(
                            task_id=str(getattr(command, "task_id", "") or ""),
                            agent_run_id=str(getattr(command, "agent_run_id", "") or ""),
                            session_id=session_id,
                            capture_id=visual_capture_id,
                            page_url=visual_ready.url,
                            page_type=detail.page_type,
                            deadline=command.deadline,
                            allow_scroll=(
                                detail_primary is not None
                                and getattr(detail_primary, "status", None) == "captured"
                            ),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - 可选补判失败不改变详情终态
                        logging.getLogger(__name__).exception("Web 详情播放器视觉补判失败")
        return WebMenuVisitResult(
            final_capture_id=readiness.ready_capture_id,
            page_type=page.page_type,
            status=detail.status,
            reused_capture_id=detail.reused_capture_id,
            reused_logical_page_id=detail.reused_logical_page_id,
            detail_capture_id=detail.detail_capture_id,
            error_code=detail.error_code,
            menu_probe_capture_id=probe.final_capture_id,
            menu_ready_capture_id=classification_capture_id,
            menu_page_type=getattr(page, "raw_page_type", page.page_type),
            menu_readiness_evidence_capture_ids=readiness.evidence_capture_ids,
            menu_dismissed_layers=readiness.dismissed_layers,
            detail_candidate_ids=detail.detail_candidate_ids,
            selected_detail_candidate_id=detail.selected_candidate_id,
            detail_selection_confidence=detail.detail_selection_confidence,
            detail_selection_reason=detail.detail_selection_reason,
            detail_selection_diagnostic=detail.detail_selection_diagnostic,
            detail_probe_capture_id=detail.probe_capture_id,
            detail_ready_capture_id=detail.detail_capture_id,
            detail_page_type=detail.page_type,
            detail_readiness_evidence_capture_ids=(detail.readiness_evidence_capture_ids),
            detail_dismissed_layers=detail.dismissed_layers,
            detail_action_count=detail.detail_action_count,
            detail_intent_id=detail.detail_intent_id,
            detail_action_execution_id=detail.detail_action_execution_id,
            detail_interaction_outcome=detail.interaction_outcome,
            detail_scroll_interaction_outcomes=detail.scroll_interaction_outcomes,
            detail_dismissal_interaction_outcomes=detail.readiness_interaction_outcomes,
            diagnostic_type=detail.diagnostic_type,
            diagnostic_message=detail.diagnostic_message,
            **menu_lineage,
        )

    async def _classify_primary_content_once(
        self,
        *,
        command: object,
        entry: object,
        ready_capture: object,
        initial_assessment: object,
    ) -> tuple[object, object | None, str | None]:
        """首屏待复核时，仅用同 URL 主体补图再判一次。"""

        proposed = str(getattr(initial_assessment, "raw_page_type", "") or "")
        canonical_types = WEB_LIST_PAGE_TYPES | WEB_DETAIL_PAGE_TYPES
        if proposed not in canonical_types:
            hint = getattr(ready_capture, "classification_hint", None) or getattr(
                entry,
                "classification_hint",
                None,
            )
            proposed = str(getattr(hint, "expected_page_type", "") or "")
        if proposed not in canonical_types:
            return initial_assessment, None, None
        primary_result = await self._capture_primary_content(
            command=command,
            ready_capture=ready_capture,
            page_type=proposed,
        )
        evidence_capture_id = str(getattr(primary_result, "evidence_capture_id", "") or "")
        if not evidence_capture_id:
            return initial_assessment, primary_result, None
        try:
            _bind_navigation_capture(
                self._navigation_candidate_source,
                session_id=str(getattr(command, "session_id", "") or ""),
                capture_id=evidence_capture_id,
            )
            supplement = await self._ready_capture_reader.read(
                capture_id=evidence_capture_id,
                page_role="menu",
            )
            if is_dataclass(supplement):
                supplement = replace(
                    supplement,
                    classification_hint=getattr(entry, "classification_hint", None),
                    menu_entry_ids=(str(getattr(entry, "entry_id", "")),),
                )
            return (
                await self._classifier.classify(supplement),
                primary_result,
                evidence_capture_id,
            )
        except Exception as exc:  # noqa: BLE001 - 已发生的补图不因补判失败丢失
            return (
                initial_assessment,
                _primary_followup_failed(primary_result, ready_capture.capture_id, exc),
                None,
            )

    async def _capture_primary_content(
        self,
        *,
        command: object,
        ready_capture: object,
        page_type: str,
    ) -> object | None:
        """主体补图是同逻辑页的可降级附件，可供一次有界补判。"""

        if self._primary_content_capture is None:
            return None
        page_url = str(getattr(ready_capture, "url", "") or "")
        if not page_url:
            return None
        try:
            return await self._primary_content_capture.capture(
                task_id=str(getattr(command, "task_id", "") or ""),
                agent_run_id=str(getattr(command, "agent_run_id", "") or ""),
                session_id=str(getattr(command, "session_id", "") or ""),
                capture_id=str(getattr(ready_capture, "capture_id", "") or ""),
                page_url=page_url,
                page_type=page_type,
                deadline=command.deadline,
            )
        except Exception as exc:  # noqa: BLE001 - 降级时仍返回固定审计，不吞掉补图失败
            return primary_content_failure(str(getattr(ready_capture, "capture_id", "") or ""), exc)


def _primary_followup_failed(result, capture_id, exc):
    if is_dataclass(result):
        return replace(
            result,
            reason_code="WEB_PRIMARY_CONTENT_FOLLOWUP_FAILED",
            diagnostic_type=type(exc).__name__,
        )
    return result if result is not None else primary_content_failure(capture_id, exc)


@dataclass(frozen=True, kw_only=True)
class WebV2Runtime:
    """Web 首跳编排与同生命周期 Capture Runtime。"""

    bootstrapper: object
    capture_runtime: object
    policy_snapshot: WebRunPolicySnapshot | None = None
    deadline: datetime | None = None
    explorer: object | None = None
    age_gate_handler: object | None = None
    age_evidence_reader: object | None = None
    pre_gate_supplemental_capture: object | None = None
    page_readiness: object | None = None
    web_login_branch: object | None = None
    navigation_context: RuntimeWebNavigationContext | None = None
    navigation_candidate_source: object | None = None
    budget_ledger: WebExplorationBudgetLedgerPort | None = None
    home_ready_capture_reader: object | None = None
    page_type_classifier: object | None = None
    primary_content_capture: WebPrimaryContentCaptureService | None = None
    player_visual_recheck: WebPlayerVisualRecheckService | None = None
    ad_landing_scheduler: WebAdLandingEvidenceScheduler | None = None
    web_guide_dispatcher: WebGuideBranchDispatcher | None = None
    ad_snapshot_reader: object | None = None
    ad_identity_policy: object | None = None
    ad_registered_domain: str | None = None
    auxiliary_navigation_explorer: object | None = None
    stage_orchestrator: object | None = None
    stage_ready_capture_reader: object | None = None
    stage_ad_capture_port: object | None = None
    defer_session_close_to_reconciliation: bool = False
    stage_detail_navigator: object | None = None
    stage_detail_evidence_source: object | None = None

    async def start(self, command: RunWebWorkflowCommand) -> object:
        if self.budget_ledger is None:
            return await self.bootstrapper.start(command)
        if self.navigation_context is None:
            raise RuntimeError("Web 持久化预算要求冻结菜单 Registry")
        context = self.navigation_context.resolve_site_context(command.target_url)
        await self.bootstrapper.open_session(command)
        await self.budget_ledger.bootstrap(
            command,
            context,
            catalog_version=context.site.catalog_version,
            catalog_sha256=context.content_sha256,
        )
        return await self.bootstrapper.navigate_home(command)

    async def prepare_home(
        self,
        command: RunWebWorkflowCommand,
        *,
        adult_self_attestation: AdultSelfAttestation | None,
    ) -> WebHomePreparationResult:
        if self.budget_ledger is not None and (
            self.home_ready_capture_reader is None or self.page_type_classifier is None
        ):
            raise WebRuntimeConfigurationError("WEB_HOME_PAGE_TYPE_ASSESSMENT_REQUIRED")
        site = None
        if self.budget_ledger is not None:
            if self.navigation_context is None:
                raise RuntimeError("Web 持久化预算要求冻结菜单 Registry")
            site = self.navigation_context.resolve_site_context(command.target_url).site
        coordination = await self.start(command)
        entry_capture_id = str(
            getattr(getattr(coordination, "after_capture", None), "capture_id", "") or ""
        )
        if not entry_capture_id:
            raise RuntimeError("WEB_EVIDENCE_INCOMPLETE: 首跳没有动作后 Capture")
        _bind_navigation_capture(
            self.navigation_candidate_source,
            session_id=command.session_id,
            capture_id=entry_capture_id,
        )
        if (
            self.age_gate_handler is None
            or self.age_evidence_reader is None
            or self.page_readiness is None
        ):
            result = WebHomePreparationResult(
                coordination=coordination,
                ready_capture_id=entry_capture_id,
            )
            await self._finalize_home(command, site=site, result=result)
            return result
        entry_evidence = await self.age_evidence_reader.read(capture_id=entry_capture_id)
        if self.pre_gate_supplemental_capture is not None:
            from services.api.app.application.web_runtime_evidence import (
                supplement_web_age_evidence,
            )

            entry_evidence = await supplement_web_age_evidence(
                entry_evidence, service=self.pre_gate_supplemental_capture
            )
        age_result = await self.age_gate_handler.handle(
            entry_evidence,
            adult_self_attestation=adult_self_attestation,
        )
        page_probe_id = age_result.after_capture_id or entry_capture_id
        readiness = await self.page_readiness.prepare(
            source_capture_id=page_probe_id,
            page_role="home",
            age_gate_verified_absent_capture_id=(
                page_probe_id if age_result.gate_disappeared else None
            ),
        )
        if not web_readiness_result_is_ready(readiness):
            raise RuntimeError(f"WEB_HOME_{readiness.status.upper()}")
        _bind_navigation_capture(
            self.navigation_candidate_source,
            session_id=command.session_id,
            capture_id=readiness.ready_capture_id,
        )
        result = WebHomePreparationResult(
            coordination=coordination,
            ready_capture_id=readiness.ready_capture_id,
            age_gate_result=age_result,
            readiness_result=readiness,
        )
        if self.home_ready_capture_reader is not None and self.page_type_classifier is not None:
            home_capture = await self.home_ready_capture_reader.read(
                capture_id=readiness.ready_capture_id,
                page_role="home",
            )
            if (
                is_dataclass(home_capture)
                and readiness.ready_capture_id == entry_evidence.capture_id
                and entry_evidence.supplemental_screenshot_artifact_ids
                and not getattr(
                    home_capture,
                    "supplemental_screenshot_artifact_ids",
                    (),
                )
            ):
                # 无年龄门时入口 Capture 可直接成为首页 ready Capture；复用已在
                # pre-gate 阶段取得的同父长图，避免一次性 claim 被解释为缺证。
                home_capture = replace(
                    home_capture,
                    supplemental_screenshot_artifact_ids=(
                        entry_evidence.supplemental_screenshot_artifact_ids
                    ),
                    supplemental_evidence_status="captured",
                    supplemental_evidence_reason_code=None,
                )
            structure = getattr(home_capture, "structure_facts", None)
            product_capability_assessment = (
                assess_web_product_capabilities(
                    readiness_status=str(getattr(readiness, "status", "stable")),
                    age_gate_present=False,
                    business_visible=True,
                    navigation_element_count=structure.navigation_element_count,
                    repeated_card_count=structure.repeated_primary_card_count,
                    video_element_count=(
                        structure.video_element_count + structure.player_root_count
                    ),
                    article_body_count=structure.article_body_count,
                    advertisement_element_count=(structure.advertisement_element_count),
                    transaction_element_count=structure.transaction_element_count,
                )
                if structure is not None
                else None
            )
            home_entry = None
            if site is not None:
                home_entry = next(
                    (
                        entry
                        for entry in site.entries
                        if entry.decision is WebMenuDecision.HOME_BASELINE
                    ),
                    None,
                )
                if home_entry is not None and is_dataclass(home_capture):
                    home_capture = replace(
                        home_capture,
                        classification_hint=home_entry.classification_hint,
                        menu_entry_ids=(home_entry.entry_id,),
                    )
            home_assessment = await self.page_type_classifier.classify(home_capture)
            home_page_type = getattr(home_assessment, "page_type", None)
            ad_landing_evidence: Mapping[str, object] = {}
            if (
                self.ad_landing_scheduler is not None
                and self.ad_snapshot_reader is not None
                and self.ad_identity_policy is not None
                and self.ad_registered_domain is not None
            ):
                try:
                    ad_result = await schedule_ready_web_business_page_ad_evidence(
                        scheduler=self.ad_landing_scheduler,
                        snapshot_reader=self.ad_snapshot_reader,
                        identity_policy=self.ad_identity_policy,
                        registered_domain=self.ad_registered_domain,
                        task_id=command.task_id,
                        agent_run_id=command.agent_run_id,
                        session_id=command.session_id,
                        page_role="home",
                        page_type=home_page_type,
                        ready_capture=home_capture,
                        evidence_capture_ids=readiness.evidence_capture_ids,
                    )
                    ad_landing_evidence = ad_result.to_dict()
                except Exception:  # noqa: BLE001 - 广告附件失败不得抹除首页基线
                    ad_landing_evidence = {
                        "status": "scheduling_failed",
                        "fail_closed": True,
                        "reason_code": "WEB_AD_EVIDENCE_SCHEDULING_FAILED",
                    }
            classification_capture_id = readiness.ready_capture_id
            primary_result = None
            proposed_page_type = str(getattr(home_assessment, "raw_page_type", "") or "")
            if (
                proposed_page_type not in (WEB_LIST_PAGE_TYPES | WEB_DETAIL_PAGE_TYPES | {"home"})
                and home_entry is not None
            ):
                proposed_page_type = str(
                    getattr(
                        getattr(home_entry, "classification_hint", None),
                        "expected_page_type",
                        "",
                    )
                    or ""
                )
            if (
                self.primary_content_capture is not None
                and home_page_type is None
                and proposed_page_type in (WEB_LIST_PAGE_TYPES | WEB_DETAIL_PAGE_TYPES | {"home"})
            ):
                try:
                    primary_result = await self.primary_content_capture.capture(
                        task_id=command.task_id,
                        agent_run_id=command.agent_run_id,
                        session_id=command.session_id,
                        capture_id=home_capture.capture_id,
                        page_url=home_capture.url,
                        page_type=proposed_page_type,
                        deadline=command.deadline,
                    )
                    supplement_capture_id = str(
                        getattr(primary_result, "evidence_capture_id", "") or ""
                    )
                    if supplement_capture_id:
                        _bind_navigation_capture(
                            self.navigation_candidate_source,
                            session_id=command.session_id,
                            capture_id=supplement_capture_id,
                        )
                        supplement = await self.home_ready_capture_reader.read(
                            capture_id=supplement_capture_id,
                            page_role="home",
                        )
                        if home_entry is not None and is_dataclass(supplement):
                            supplement = replace(
                                supplement,
                                classification_hint=home_entry.classification_hint,
                                menu_entry_ids=(home_entry.entry_id,),
                            )
                        home_assessment = await self.page_type_classifier.classify(supplement)
                        home_page_type = getattr(home_assessment, "page_type", None)
                        classification_capture_id = supplement_capture_id
                except Exception as exc:  # noqa: BLE001 - 保留首屏及已发生补图，补判失败单独留痕
                    primary_result = _primary_followup_failed(
                        primary_result, home_capture.capture_id, exc
                    )
            if (
                self.primary_content_capture is not None
                and primary_result is None
                and home_page_type == "home"
            ):
                try:
                    primary_result = await self.primary_content_capture.capture(
                        task_id=command.task_id,
                        agent_run_id=command.agent_run_id,
                        session_id=command.session_id,
                        capture_id=home_capture.capture_id,
                        page_url=home_capture.url,
                        page_type=home_page_type,
                        deadline=command.deadline,
                    )
                except Exception as exc:  # noqa: BLE001 - 首页补图异常进入同一 HOME 终态
                    primary_result = primary_content_failure(home_capture.capture_id, exc)
            result = WebHomePreparationResult(
                coordination=coordination,
                ready_capture_id=classification_capture_id,
                age_gate_result=age_result,
                readiness_result=readiness,
                page_type=getattr(home_assessment, "page_type", None),
                raw_page_type=getattr(home_assessment, "raw_page_type", None),
                product_capability_assessment=product_capability_assessment,
                primary_content_diagnostics=(
                    (primary_content_diagnostic(primary_result),)
                    if primary_result is not None
                    else ()
                ),
                ad_landing_evidence=ad_landing_evidence,
            )
        if self.primary_content_capture is not None:
            try:
                current_id = await _read_navigation_handoff(
                    self.home_ready_capture_reader,
                    self.navigation_candidate_source,
                    session_id=command.session_id,
                    page_role="home",
                )
                result = replace(result, navigation_current_capture_id=current_id)
            except Exception:  # noqa: BLE001 - 首页基线仍落账本，但不以陈旧基线继续执行
                result = replace(
                    result, navigation_handoff_error="WEB_NAVIGATION_CURRENT_UNTRUSTED"
                )
        await self._finalize_home(command, site=site, result=result)
        return result

    async def _finalize_home(
        self,
        command: RunWebWorkflowCommand,
        *,
        site: WebSiteMenuCatalog | None,
        result: WebHomePreparationResult,
    ) -> None:
        if self.budget_ledger is None or site is None:
            return
        home = next(
            entry for entry in site.entries if entry.decision is WebMenuDecision.HOME_BASELINE
        )
        probe_capture_id = str(
            getattr(getattr(result.coordination, "after_capture", None), "capture_id", "") or ""
        )
        legacy_policy = self.policy_snapshot is not None and not any(
            item.get("code") == "home"
            for item in self.policy_snapshot.payload["page_catalog"]["page_types"]
        )
        is_recognized = result.page_type == "home" or (
            legacy_policy and result.page_type in (WEB_LIST_PAGE_TYPES | WEB_DETAIL_PAGE_TYPES)
        )
        await self.budget_ledger.finalize(
            WebMenuLedgerTerminalRecord(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                site_id=site.site_id,
                menu_entry_id=home.entry_id,
                status=(
                    WebMenuTerminalStatus.HOME_BASELINE
                    if is_recognized
                    else WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED
                ),
                menu_probe_capture_id=probe_capture_id,
                menu_ready_capture_id=result.ready_capture_id,
                menu_page_type=result.page_type if is_recognized else None,
                diagnostic={
                    **({} if is_recognized else {"error_code": "WEB_PAGE_TYPE_UNCLASSIFIED"}),
                    **(
                        {"navigation_handoff_error": result.navigation_handoff_error}
                        if getattr(result, "navigation_handoff_error", None)
                        else {}
                    ),
                    **(
                        {"primary_content_diagnostics": list(result.primary_content_diagnostics)}
                        if getattr(result, "primary_content_diagnostics", ())
                        else {}
                    ),
                    **(
                        {"ad_landing_evidence": dict(result.ad_landing_evidence)}
                        if getattr(result, "ad_landing_evidence", {})
                        else {}
                    ),
                },
            )
        )

    async def read_persisted_exploration(
        self,
        command: RunWebWorkflowCommand,
    ) -> WebDesktopExplorationSummary:
        if self.budget_ledger is None or self.navigation_context is None:
            raise RuntimeError("Web 持久化预算账本未配置")
        context = self.navigation_context.resolve_site_context(command.target_url)
        site = context.site
        summary = await self.budget_ledger.read_summary(
            agent_run_id=command.agent_run_id,
            site_id=site.site_id,
        )
        return hydrate_web_menu_summary(
            summary,
            site=site,
            catalog_version=site.catalog_version,
            catalog_sha256=context.content_sha256,
        )

    async def close(self, *, task_id: str, session_id: str) -> None:
        await self.capture_runtime.close_exploration_session(
            task_id=task_id,
            session_id=session_id,
        )

    async def explore(
        self,
        command: RunWebWorkflowCommand,
        *,
        initial_capture_id: str,
        initial_page_type: str | None = None,
        initial_navigation_capture_id: str | None = None,
    ) -> WebDesktopExplorationSummary:
        if self.explorer is None:
            return WebDesktopExplorationSummary(
                page_timeline=(initial_capture_id,),
                page_type_keys=((initial_page_type,) if initial_page_type is not None else ()),
            )
        summary = await self.explorer.explore(
            command,
            initial_capture_id=initial_capture_id,
            initial_page_type=initial_page_type,
            **(
                {"initial_navigation_capture_id": initial_navigation_capture_id}
                if initial_navigation_capture_id
                else {}
            ),
        )
        auxiliary_source_capture_id = str(
            getattr(summary, "navigation_current_capture_id", "") or ""
        )
        if self.auxiliary_navigation_explorer is not None and auxiliary_source_capture_id:
            try:
                explore_all = getattr(self.auxiliary_navigation_explorer, "explore_all", None)
                if callable(explore_all):
                    auxiliary_results = tuple(
                        await explore_all(
                            command,
                            source_capture_id=auxiliary_source_capture_id,
                        )
                    )
                else:
                    auxiliary_results = (
                        await self.auxiliary_navigation_explorer.explore(
                            command,
                            source_capture_id=auxiliary_source_capture_id,
                        ),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 辅助入口失败不能抹除已完成的菜单账本
                auxiliary_results = ()
            for auxiliary in auxiliary_results:
                auxiliary_capture_id = str(getattr(auxiliary, "ready_capture_id", "") or "")
                auxiliary_current_capture_id = str(
                    auxiliary_capture_id or getattr(auxiliary, "probe_capture_id", "") or ""
                )
                auxiliary_page_type = str(getattr(auxiliary, "page_type", "") or "")
                if (
                    auxiliary_capture_id
                    and auxiliary_capture_id not in summary.page_timeline
                    and is_dataclass(summary)
                ):
                    summary = replace(
                        summary,
                        page_timeline=(*summary.page_timeline, auxiliary_capture_id),
                        page_type_keys=(
                            *summary.page_type_keys,
                            *((auxiliary_page_type,) if auxiliary_page_type else ()),
                        ),
                        navigation_current_capture_id=auxiliary_capture_id,
                    )
                elif auxiliary_current_capture_id and is_dataclass(summary):
                    summary = replace(
                        summary,
                        navigation_current_capture_id=auxiliary_current_capture_id,
                    )
        if self.budget_ledger is not None:
            if self.navigation_context is None:
                raise RuntimeError("Web 持久化预算要求冻结菜单 Registry")
            site = self.navigation_context.resolve_site_context(command.target_url).site
            await self.budget_ledger.seal(command=command, site_id=site.site_id)
        return summary

    async def explore_auxiliary(
        self,
        command: RunWebWorkflowCommand,
        *,
        source_capture_id: str,
        candidate: object | None = None,
    ) -> object:
        """执行 Web 专属 href 辅助探索；不复用或改写普通菜单动作语义。"""

        if self.auxiliary_navigation_explorer is None:
            raise WebRuntimeConfigurationError("WEB_AUXILIARY_NAVIGATION_NOT_CONFIGURED")
        return await self.auxiliary_navigation_explorer.explore(
            command,
            source_capture_id=source_capture_id,
            candidate=candidate,
        )


class PostgresWebRuntimeProvider:
    """为 Web Run 组合独立 Session Owner 和无 H5 组件的浏览器 Runtime。"""

    def __init__(
        self,
        *,
        capture_runtime_builder: Callable[..., object] = build_default_web_capture_runtime,
        assessment_agent: object | None = None,
        page_inspection_notification: object | None = None,
        formal_session_closer: DurableWebFormalSessionCloser | None = None,
        policy_repository: object | None = None,
        frontier_capture_metadata_reader: object | None = None,
        frontier_ready_capture_reader: object | None = None,
    ) -> None:
        self._capture_runtime_builder = capture_runtime_builder
        self._assessment_agent = assessment_agent
        self._policy_repository = policy_repository or PostgresWebRunPolicySnapshotRepository()
        self._page_inspection_notification = page_inspection_notification
        self._formal_session_closer = formal_session_closer
        self._frontier_capture_metadata_reader = frontier_capture_metadata_reader
        self._frontier_ready_capture_reader = frontier_ready_capture_reader

    async def build(
        self,
        *,
        record: AgentRunRecord,
        settings: Settings,
    ) -> WebV2Runtime:
        from services.api.app.domain.exploration.web_runtime_contract import (
            WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED,
            is_legacy_web_execution,
        )

        if is_legacy_web_execution(record.task_config):
            raise WebRunPolicyError(WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED)
        task_id = _required_task_id(record)
        target_url, _profile_id = _locked_web_runtime_config(record)
        snapshot = await self._policy_repository.read(task_id=task_id, agent_run_id=record.run_id)
        if snapshot is None:
            snapshot = await self._policy_repository.freeze_or_read(
                WebRunPolicySnapshot.create(
                    task_id=task_id,
                    agent_run_id=record.run_id,
                    payload=load_published_web_policy(
                        now=datetime.now(UTC),
                        max_duration_seconds=_max_duration_seconds(record, settings),
                        exploration_target=record.task_config.get("web_exploration_target"),
                    ),
                )
            )
        if snapshot.task_id != task_id or snapshot.agent_run_id != record.run_id:
            raise WebRunPolicyError("WEB_POLICY_OWNERSHIP_INVALID: Runtime 与冻结正文归属不符")
        policy = materialize_web_policy(snapshot)
        navigation_context = build_new_web_navigation_context(target_url=target_url, policy=policy)
        site_context = navigation_context.resolve_site_context(target_url)
        registered_domain = str(urlsplit(site_context.site.origin).hostname or "")
        if not registered_domain:
            raise WebRuntimeConfigurationError("WEB_AD_REGISTERED_DOMAIN_MISSING")
        deadline = policy.deadline
        capture_runtime: WebCaptureRuntime = self._capture_runtime_builder(
            session_owner=WEB_SURFACE_SESSION_OWNER,
            include_h5_exploration=False,
            h5_visual_coordinate_mode="off",
            logical_surface_enabled=False,
            logical_surface_shadow=False,
            logical_targets_enabled=False,
            web_desktop_policy_enabled=True,
            web_readiness_profile=policy.readiness_profile,
            web_run_policy=snapshot,
            evidence_exit_gate=_build_payload_exit_gate(settings),
        )
        if capture_runtime.surface_driver is None or capture_runtime.surface_sessions is None:
            raise WebRuntimeConfigurationError(
                "WEB_RUNTIME_NOT_CONFIGURED: 浏览器 Driver 或 Session Repository 未配置"
            )
        raw_candidate_source = capture_runtime.web_desktop_capture_policy
        deferred_discovery = getattr(
            raw_candidate_source,
            "defer_navigation_discovery_until_authorized",
            None,
        )
        if callable(deferred_discovery):
            deferred_discovery()
        catalog_identity = (
            WebPageTypeCatalogIdentity.from_frozen_catalog_payload(
                snapshot.to_payload()["page_catalog"]
            )
            if policy.assessment_schema_version == "web-page-type-assessment.v4"
            else None
        )
        capture_evidence_repository = PostgresWebCaptureEvidenceRepository(
            assessment_schema_version=policy.assessment_schema_version,
            catalog_identity=catalog_identity,
        )
        from services.api.app.infrastructure.db.web_logical_target_repository import (
            PostgresWebLogicalTargetRepository,
        )

        logical_targets = PostgresWebLogicalTargetRepository()
        evidence_source = WebRuntimeEvidenceSource(
            task_id=task_id,
            agent_run_id=record.run_id,
            session_id=f"web:{record.run_id}",
            deadline=deadline,
            index_reader=capture_evidence_repository,
            snapshot_reader=capture_runtime.trusted_ui_snapshot_reader,
            readiness_profile=policy.readiness_profile,
            identity_policy=policy.identity_policy,
            logical_target_repository=logical_targets,
        )
        assessment_agent = self._assessment_agent or WebExplorationAssessmentAgent(
            workflow_skill_content=policy.workflow_skill,
            client=build_chat_client(
                settings.llm,
                usage_source="web_exploration_assessment_agent",
                usage_operation="web_read_only_assessment",
            ),
            evidence_loader=VerifiedWebAssessmentEvidenceLoader(
                metadata_reader=PostgresWebAssessmentArtifactMetadataReader(),
                artifact_store=build_evidence_artifact_store(settings.evidence_storage),
            ),
        )
        age_evidence_reader = WebAgeEvidenceReaderAdapter(source=evidence_source)
        page_evidence_reader = WebPageEvidenceReaderAdapter(source=evidence_source)
        ready_capture_reader: object = WebReadyCaptureReaderAdapter(source=evidence_source)
        stage_ready_capture_reader = ready_capture_reader
        candidate_source = TrustedWebFrontierCandidateSource(
            delegate=raw_candidate_source,
            capture_metadata_reader=(
                self._frontier_capture_metadata_reader
                or WebFrontierCaptureMetadataReaderAdapter(source=evidence_source)
            ),
            ready_capture_reader=self._frontier_ready_capture_reader or ready_capture_reader,
            frozen_policy=policy,
            target_url=target_url,
            session_id=f"web:{record.run_id}",
        )
        supplemental_capture_service = None
        if policy.full_page_supplemental_policy.enabled:
            supplemental_capture_service = WebFullPageSupplementalCaptureService(
                browser=capture_runtime.web_full_page_capture_browser,
                persistence=capture_runtime.web_page_capture_persistence,
                claims=PostgresWebFullPageSupplementalClaimRepository(),
                session_command_gate=capture_runtime.session_command_gate,
                stabilization_policy=policy.full_page_supplemental_policy.stabilization,
            )
            ready_capture_reader = WebFullPageSupplementingReadyCaptureReader(
                delegate=ready_capture_reader,
                supplemental_capture_service=supplemental_capture_service,
                task_id=task_id,
                agent_run_id=record.run_id,
                session_id=f"web:{record.run_id}",
                deadline=deadline,
            )
        age_gate_handler = WebAgeGateHandler(
            assessor=assessment_agent,
            coordinator=capture_runtime.coordinator,
            evidence_reader=age_evidence_reader,
            page_type_recorder=(
                WebAgeGatePageTypeRecorder(
                    repository=capture_evidence_repository,
                    evidence_reader=age_evidence_reader,
                    catalog_identity=catalog_identity,
                )
                if policy.assessment_schema_version == "web-page-type-assessment.v4"
                else None
            ),
        )
        notification = None
        if _formal_web_notification_enabled(record.task_config):
            notification = self._page_inspection_notification or (
                build_default_web_page_inspection_notification(settings=settings)
            )

        popup_blocked_repository = PostgresWebPopupBlockedRepository()

        async def record_popup_blocked(**kwargs):
            return await _record_popup_blocked_best_effort(
                popup_blocked_repository,
                task_id=task_id,
                agent_run_id=record.run_id,
                session_id=f"web:{record.run_id}",
                **kwargs,
            )

        async def notify_failure_sequence(**kwargs):
            return await notification.notify_readiness_failure(
                task_id=task_id,
                agent_run_id=record.run_id,
                session_id=f"web:{record.run_id}",
                **kwargs,
            )

        async def notify_interaction_sequence(**kwargs):
            return await notification.notify_readiness_interaction(
                task_id=task_id,
                agent_run_id=record.run_id,
                session_id=f"web:{record.run_id}",
                **kwargs,
            )

        page_readiness = WebPageReadinessCoordinator(
            assessor=assessment_agent,
            coordinator=capture_runtime.coordinator,
            evidence_reader=page_evidence_reader,
            age_gate_handler=age_gate_handler,
            adult_self_attestation=_adult_self_attestation(record),
            popup_blocked_recorder=record_popup_blocked,
            failure_sequence_notifier=notify_failure_sequence if notification is not None else None,
            interaction_sequence_notifier=notify_interaction_sequence
            if notification is not None
            else None,
        )
        web_login_branch = None
        if os.getenv("ALIOTH_WEB_LOGIN_ENABLED", "").casefold() in {"1", "true", "yes"}:
            from services.api.app.agents.explorer.web_login_agent import (
                WebLoginLocalVisualAgent,
            )
            from services.api.app.application.web_login_account_inspection import (
                WebLoginAccountInspector,
            )
            from services.api.app.application.web_login_allowlist import (
                WebLoginAllowlistError,
                load_web_login_allowlist,
            )
            from services.api.app.application.web_login_artifact_index import (
                WebLoginIndexedArtifactStore,
            )
            from services.api.app.application.web_login_auth_origins import (
                DEFAULT_WEB_LOGIN_AUTH_ORIGINS_PATH,
                WebLoginAuthOriginsError,
                load_web_login_auth_origins,
            )
            from services.api.app.application.web_login_branch import WebLoginBranch
            from services.api.app.application.web_login_screenshot_ocr import (
                WebLoginScreenshotOcr,
            )
            from services.api.app.infrastructure.db.web_login_artifact_index_repository import (
                PostgresWebLoginArtifactIndexRepository,
            )

            allowlist_path = os.getenv("ALIOTH_WEB_LOGIN_ALLOWLIST_PATH", "")
            if not allowlist_path:
                raise WebRuntimeConfigurationError("WEB_LOGIN_ALLOWLIST_PATH_MISSING")
            try:
                allowlist = load_web_login_allowlist(allowlist_path)
            except WebLoginAllowlistError as exc:
                raise WebRuntimeConfigurationError(str(exc)) from None
            try:
                auth_policy = load_web_login_auth_origins(
                    DEFAULT_WEB_LOGIN_AUTH_ORIGINS_PATH, allowlist=allowlist
                )
            except WebLoginAuthOriginsError as exc:
                raise WebRuntimeConfigurationError(str(exc)) from None
            login_browser = capture_runtime.create_web_login_session_browser(
                auth_policy=auth_policy
            )
            web_login_branch = WebLoginBranch(
                allowlist=allowlist,
                browser=login_browser,
                inspector=WebLoginAccountInspector(
                    browser=login_browser,
                    artifact_store=WebLoginIndexedArtifactStore(
                        object_store=capture_runtime.web_login_artifact_store,
                        index=PostgresWebLoginArtifactIndexRepository(),
                        agent_run_id=record.run_id,
                        main_session_id=f"web:{record.run_id}",
                    ),
                    ocr=WebLoginScreenshotOcr(),
                ),
                visual=WebLoginLocalVisualAgent(),
            )
        classifier = WebPageClassifier(
            assessor=assessment_agent,
            catalog=policy.catalog,
        )
        page_type_recorder = WebPageTypeAssessmentRecorder(
            repository=capture_evidence_repository,
            assessment_schema_version=policy.assessment_schema_version,
            identity_policy=policy.identity_policy,
            judgement_ledger=PageTypeJudgementLedger(
                repository=PostgresPageTypeJudgementRepository()
            ),
            catalog_identity=catalog_identity,
        )
        recording_classifier: object = WebPageTypeAssessmentRecordingClassifier(
            classifier=classifier,
            recorder=page_type_recorder,
            task_id=task_id,
            agent_run_id=record.run_id,
            session_id=f"web:{record.run_id}",
        )
        if settings.web_evidence_exit_gate.mode != "off":
            from services.api.app.application.web_capture_preservation import WebPayloadClassifying
            from services.api.app.infrastructure.db.web_payload_readiness_repository import (
                PostgresWebPayloadReadinessRepository,
            )

            recording_classifier = WebPayloadClassifying(
                classifier=recording_classifier,
                repository=PostgresWebPayloadReadinessRepository(
                    artifact_store=build_evidence_artifact_store(settings.evidence_storage)
                ),
                task_id=task_id,
                agent_run_id=record.run_id,
                session_id=f"web:{record.run_id}",
            )
        if _formal_web_notification_enabled(record.task_config):
            recording_classifier = WebPageInspectionNotifyingClassifier(
                classifier=recording_classifier,
                notification=notification,
                task_id=task_id,
                agent_run_id=record.run_id,
                session_id=f"web:{record.run_id}",
            )
        if self._formal_session_closer is not None and _formal_web_notification_enabled(
            record.task_config
        ):
            self._formal_session_closer.bind_live_runtime(
                session_id=f"web:{record.run_id}",
                runtime=capture_runtime,
            )
        from services.api.app.application.web_logical_target import (
            WebLogicalTargetClassifier,
            WebLogicalTargetPrimaryCapture,
        )

        recording_classifier = WebLogicalTargetClassifier(
            classifier=recording_classifier,
            repository=logical_targets,
            task_id=task_id,
            agent_run_id=record.run_id,
            session_id=f"web:{record.run_id}",
        )
        budget_ledger = PostgresWebExplorationBudgetLedgerRepository(frozen_policy=policy)
        persisted_navigation = None
        if os.getenv("ALIOTH_DATABASE_URL"):
            try:
                persisted_navigation = await budget_ledger.read_summary(
                    agent_run_id=record.run_id,
                    site_id=navigation_context.resolve_site_context(target_url).site.site_id,
                )
            except WebExplorationBudgetLedgerConflictError:
                persisted_navigation = None
        if persisted_navigation is not None:
            # 阶段续跑可能跳过 bootstrap，恢复候选前仍须核对实际账本与冻结额度。
            require_budget_policy_match({"menu_limit": persisted_navigation.menu_limit}, policy)
        if persisted_navigation is not None and policy.discovery_policy.enabled:
            restore_persisted_navigation_state(
                candidate_source,
                persisted_navigation,
                session_id=f"web:{record.run_id}",
            )
        raw_primary_content_capture = WebPrimaryContentCaptureService(
            coordinator=capture_runtime.coordinator,
            evidence_source=evidence_source,
            metadata_writer=capture_evidence_repository,
            obstruction_assessor=assessment_agent,
            identity_policy=policy.identity_policy,
        )
        primary_content_capture = WebLogicalTargetPrimaryCapture(
            service=raw_primary_content_capture, repository=logical_targets
        )
        player_visual_recheck = WebPlayerVisualRecheckService(
            primary_capture=raw_primary_content_capture,
            evidence_source=evidence_source,
            assessor=assessment_agent,
            writer=capture_evidence_repository,
            identity_policy=policy.identity_policy,
        )
        detail_navigator = WebListDetailNavigator(
            selector=assessment_agent,
            coordinator=capture_runtime.coordinator,
            readiness=page_readiness,
            ready_capture_reader=ready_capture_reader,
            classifier=recording_classifier,
            capture_url_reader=evidence_source,
            budget_ledger=budget_ledger,
            target_observation_reader=evidence_source,
            logical_target_repository=logical_targets,
            identity_policy=policy.identity_policy,
        )
        ad_capture_port_factory = getattr(
            capture_runtime, "build_web_ad_landing_capture_port", None
        )
        ad_capture_port = (
            ad_capture_port_factory(page_type_assessor=assessment_agent)
            if callable(ad_capture_port_factory)
            else capture_runtime.web_ad_landing_capture_port
        )
        ad_landing_scheduler = WebAdLandingEvidenceScheduler(
            capture_port=ad_capture_port,
        )
        guide_browser_factory = getattr(capture_runtime, "build_web_guide_browser", None)
        guide_dispatcher = None
        try:
            guide_browser = (
                guide_browser_factory()
                if callable(guide_browser_factory) and "guide_page" in policy.catalog.codes
                else None
            )
            if guide_browser is not None:
                guide_dispatcher = WebGuideBranchDispatcher(
                    branch=WebGuideBranch(
                        observer=guide_browser,
                        writer=WebGuideEvidenceWriter(
                            persistence=capture_runtime.web_guide_capture_persistence,
                            marker=PostgresWebGuideEvidenceMarker(),
                            gate=capture_runtime.session_command_gate,
                        ),
                        assessor=assessment_agent,
                    ),
                    ledger=WebGuidePhaseLedger(
                        gate=capture_runtime.session_command_gate,
                        phases=PostgresSurfacePhaseRepository(),
                    ),
                    task_id=task_id,
                    agent_run_id=record.run_id,
                    session_id=f"web:{record.run_id}",
                    deadline=deadline,
                )
        except Exception:  # noqa: BLE001 - 可选支线的装配故障不得中断主 Web Runtime
            logging.getLogger(__name__).warning(
                "web_guide_composition_failed run_id=%s", record.run_id,
            )
        auxiliary_navigation_explorer = WebAuxiliaryNavigationExplorer(
            coordinator=capture_runtime.coordinator,
            snapshot_reader=evidence_source,
            capture_url_reader=evidence_source,
            readiness=page_readiness,
            ready_capture_reader=ready_capture_reader,
            classifier=recording_classifier,
            claims=PostgresWebAuxiliaryNavigationClaimRepository(),
            ad_landing_scheduler=ad_landing_scheduler,
            identity_policy=policy.identity_policy,
        )
        page_visitor = WebOrchestratedMenuPageVisitor(
            navigator=WebDesktopMenuPageVisitor(
                coordinator=capture_runtime.coordinator,
                capture_url_reader=evidence_source,
            ),
            readiness=page_readiness,
            ready_capture_reader=ready_capture_reader,
            classifier=recording_classifier,
            evidence_source=evidence_source,
            detail_navigator=detail_navigator,
            primary_content_capture=primary_content_capture,
            player_visual_recheck=player_visual_recheck,
            navigation_candidate_source=candidate_source,
            ad_landing_scheduler=ad_landing_scheduler,
            ad_identity_policy=policy.identity_policy,
            ad_registered_domain=registered_domain,
        )
        runtime = WebV2Runtime(
            policy_snapshot=snapshot,
            deadline=deadline,
            bootstrapper=WebSurfaceBootstrapper(
                driver=capture_runtime.surface_driver,
                surface_sessions=capture_runtime.surface_sessions,
                capture_coordinator=capture_runtime.coordinator,
            ),
            capture_runtime=capture_runtime,
            explorer=WebMenuExplorer(
                navigation_context=navigation_context,
                candidate_source=candidate_source,
                page_visitor=page_visitor,
                budget_ledger=budget_ledger,
                logical_target_repository=logical_targets,
                discovery_policy=policy.discovery_policy,
                required_page_types=policy.required_page_types,
            ),
            age_gate_handler=age_gate_handler,
            age_evidence_reader=age_evidence_reader,
            pre_gate_supplemental_capture=supplemental_capture_service,
            page_readiness=page_readiness,
            web_login_branch=web_login_branch,
            navigation_context=navigation_context,
            navigation_candidate_source=candidate_source,
            budget_ledger=budget_ledger,
            home_ready_capture_reader=ready_capture_reader,
            page_type_classifier=recording_classifier,
            primary_content_capture=primary_content_capture,
            player_visual_recheck=player_visual_recheck,
            ad_landing_scheduler=ad_landing_scheduler,
            web_guide_dispatcher=guide_dispatcher,
            ad_snapshot_reader=evidence_source,
            ad_identity_policy=policy.identity_policy,
            ad_registered_domain=registered_domain,
            auxiliary_navigation_explorer=auxiliary_navigation_explorer,
            stage_ready_capture_reader=stage_ready_capture_reader,
            stage_ad_capture_port=ad_capture_port,
            # 正式质检阶段只交接关闭责任，浏览器由最终对账在排空后关闭。
            defer_session_close_to_reconciliation=_formal_web_notification_enabled(
                record.task_config
            ),
            stage_detail_navigator=detail_navigator,
            stage_detail_evidence_source=evidence_source,
        )
        from services.api.app.application.web_runtime_stage_runner import (
            build_web_runtime_stage_runner,
        )

        return replace(
            runtime,
            stage_orchestrator=build_web_runtime_stage_runner(
                runtime=runtime, record=record, navigator=page_visitor._navigator
            ),
        )


async def _read_navigation_handoff(reader, candidate_source, *, session_id, page_role) -> str:
    """分类基线与后续动作来源独立；权威当前帧通过原就绪边界后才能绑定菜单。"""
    current = await reader.read_current(page_role=page_role)
    capture_id = str(getattr(current, "capture_id", "") or "")
    if not capture_id:
        raise RuntimeError("WEB_NAVIGATION_CURRENT_MISSING")
    _bind_navigation_capture(candidate_source, session_id=session_id, capture_id=capture_id)
    return capture_id


def _failed_detail_recovery_source(
    result: WebMenuVisitResult,
) -> tuple[str, str, bool] | None:
    """从详情失败的真实 after Capture 恢复当前页，且不重发业务动作。"""

    outcome = result.detail_interaction_outcome
    capture_id = str(getattr(outcome, "after_capture_id", "") or "")
    if (
        result.error_code in {"WEB_DETAIL_READINESS_FAILED", "WEB_DETAIL_POPUP_BLOCKED"}
        and capture_id
        and bool(getattr(outcome, "command_accepted", False))
        and bool(getattr(outcome, "effect_observed", False))
        and bool(getattr(outcome, "postcondition_satisfied", False))
        and not bool(getattr(outcome, "transition_arrived", False))
        and getattr(outcome, "failure_reason", None) == "readiness_not_accepted"
    ):
        return capture_id, "detail", False

    for diagnostic in reversed(result.primary_content_diagnostics):
        if diagnostic.get("reason_code") != "WEB_PRIMARY_CONTENT_AFTER_UNTRUSTED":
            continue
        raw_outcomes = diagnostic.get("interaction_outcomes")
        if not isinstance(raw_outcomes, list | tuple):
            continue
        for raw_outcome in reversed(raw_outcomes):
            if not isinstance(raw_outcome, Mapping):
                continue
            capture_id = str(raw_outcome.get("after_capture_id") or "")
            if (
                capture_id
                and raw_outcome.get("command_accepted") is True
                and raw_outcome.get("effect_observed") is True
                and raw_outcome.get("postcondition_satisfied") is True
                and raw_outcome.get("transition_arrived") is False
                and raw_outcome.get("failure_reason") == "readiness_not_accepted"
                and raw_outcome.get("evidence_mode") == "natural"
            ):
                return capture_id, "menu", False

    for outcome in reversed(result.detail_scroll_interaction_outcomes):
        capture_id = str(getattr(outcome, "after_capture_id", "") or "")
        if (
            capture_id
            and outcome.command_accepted
            and outcome.effect_observed
            and not outcome.postcondition_satisfied
            and not outcome.transition_arrived
        ):
            return capture_id, "menu", True
    return None


def _bind_navigation_capture(
    candidate_source: object | None,
    *,
    session_id: str,
    capture_id: str,
) -> None:
    binder = getattr(candidate_source, "bind_latest_navigation_capture", None)
    if callable(binder) and session_id and capture_id:
        binder(session_id, capture_id)


async def _record_popup_blocked_best_effort(repository, **kwargs) -> bool:
    """弹窗阻断只是诊断标记：围栏失效时放弃标记并记下原因，不改就绪结论，也不让 Run 失败。"""
    try:
        return await repository.record(**kwargs)
    except WebPopupBlockedPersistenceError as exc:
        logging.getLogger(__name__).warning(
            "Web 弹窗阻断标记未写入 task_id=%s agent_run_id=%s capture_id=%s reason=%s",
            kwargs.get("task_id"),
            kwargs.get("agent_run_id"),
            kwargs.get("capture_id"),
            exc.reason_code,
        )
        return False


def _stage_ownership_unconfirmed(error: BaseException) -> bool:
    """阶段失权后禁止旧 Worker 通过空命令租约关闭已接管的共享 Session。"""
    from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
        WebStageCheckpointConflict,
    )

    pending = [error]
    visited = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if (
            isinstance(current, WebStageCheckpointConflict)
            or str(current) == "WEB_STAGE_LEASE_LOST"
            or getattr(current, "code", None) == "WEB_STAGE_LEASE_LOST"
        ):
            return True
        pending.extend(
            item for item in (current.__cause__, current.__context__) if item is not None
        )
    return False


class ProductionWebV2RuntimeExecutor:
    """执行 Web 根页面双采；正式模式只并行通知，不等待 Judge。"""

    def __init__(
        self,
        *,
        worker_instance_id: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(worker_instance_id, str) or not worker_instance_id.strip():
            raise ValueError("worker_instance_id 不能为空")
        self._worker_instance_id = worker_instance_id.strip()
        self._now = now or (lambda: datetime.now(UTC))

    async def run(
        self,
        runtime: WebV2Runtime,
        *,
        record: AgentRunRecord,
        settings: Settings,
        progress_recorder: object | None = None,
    ) -> RunResult:
        del progress_recorder
        target_url, profile_id = _locked_web_runtime_config(record)
        adult_self_attestation = _adult_self_attestation(record)
        session_id = f"web:{record.run_id}"
        now = self._now()
        max_duration_seconds = _max_duration_seconds(record, settings)
        command = RunWebWorkflowCommand(
            task_id=_required_task_id(record),
            agent_run_id=record.run_id,
            session_id=session_id,
            session_owner=WEB_SURFACE_SESSION_OWNER,
            target_url=target_url,
            browser_profile_id=profile_id,
            deadline=getattr(runtime, "deadline", None)
            or (now + timedelta(seconds=max_duration_seconds)),
        )
        coordination: object | None = None
        preparation: WebHomePreparationResult | None = None
        exploration: WebDesktopExplorationSummary | None = None
        failure: Exception | None = None
        failure_cleanup_ready = False
        close_failure: Exception | None = None
        session_attempted = False
        stage_gap_codes = ()
        stage_orchestrator = getattr(runtime, "stage_orchestrator", None)
        try:
            if command.deadline <= now and stage_orchestrator is None:
                raise WebRunPolicyError("WEB_POLICY_DEADLINE_EXPIRED: 当前 Run 冻结期限已耗尽")
            session_attempted = True
            if stage_orchestrator is not None:
                configure_worker = getattr(stage_orchestrator, "configure_worker", None)
                if callable(configure_worker):
                    configure_worker(self._worker_instance_id)
                await stage_orchestrator.initialize(
                    command, adult_self_attestation=adult_self_attestation
                )
                while due := await stage_orchestrator.next_due(record.run_id):
                    await stage_orchestrator.advance_once(record.run_id, due.logical_page_key)
                outcome = await stage_orchestrator.final_result(record.run_id)
                preparation, exploration = outcome.preparation, outcome.exploration
                stage_gap_codes = tuple(getattr(outcome, "gap_codes", ()))
            else:
                preparation = await runtime.prepare_home(
                    command,
                    adult_self_attestation=adult_self_attestation,
                )
                coordination = preparation.coordination
                initial_capture = preparation.ready_capture_id
                if getattr(preparation, "navigation_handoff_error", None):
                    raise RuntimeError(preparation.navigation_handoff_error)
                exploration = await runtime.explore(
                    command,
                    initial_capture_id=initial_capture,
                    initial_page_type=getattr(preparation, "page_type", None),
                    **(
                        {"initial_navigation_capture_id": preparation.navigation_current_capture_id}
                        if getattr(preparation, "navigation_current_capture_id", None)
                        else {}
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - Runtime 外部故障统一转换为标准 RunResult
            failure = exc
            ownership_unconfirmed = _stage_ownership_unconfirmed(exc)
            reader = getattr(runtime, "read_persisted_exploration", None)
            if callable(reader):
                try:
                    exploration = await reader(command)
                except Exception as summary_error:  # noqa: BLE001 - 保留原始失败及失权围栏
                    ownership_unconfirmed |= _stage_ownership_unconfirmed(summary_error)
            # 摘要读取也可能被取消；只有普通异常处理完成才允许阶段外关闭。
            failure_cleanup_ready = not ownership_unconfirmed
        finally:
            guide_dispatcher = getattr(runtime, "web_guide_dispatcher", None)
            close_guide = getattr(guide_dispatcher, "close", None)
            if callable(close_guide):
                await close_guide()
            if (
                # 正常阶段由 checkpoint 关闭；失权后即使命令租约为空也不可关闭共享会话。
                # CancelledError 不转为普通失败，避免取消或接管时绕过所有权收口。
                (stage_orchestrator is None or failure_cleanup_ready)
                and session_attempted
                and record.task_config.get("execution_mode") == "exploration_only"
            ):
                try:
                    await runtime.close(task_id=command.task_id, session_id=session_id)
                except Exception as exc:  # noqa: BLE001 - 关闭失败不能伪装成功
                    close_failure = exc
                    if failure is None:
                        failure = exc

        if failure is not None:
            preserved_timeline = tuple(getattr(exploration, "page_timeline", ()) or ())
            preserved_summary: dict[str, object] = {}
            if exploration is not None:
                preserved_summary = _web_exploration_summary(
                    exploration,
                    adult_self_attestation=adult_self_attestation,
                    preparation=preparation,
                )
            if close_failure is not None:
                preserved_summary["session_close"] = {
                    "status": "failed",
                    "error_code": "WEB_SESSION_CLOSE_FAILED",
                    "diagnostic_type": _exception_type(close_failure),
                }
            close_only_failure = (
                close_failure is not None
                and failure is close_failure
                and exploration is not None
                and bool(preserved_timeline)
            )
            return RunResult(
                termination_reason=TerminationReason.ERROR,
                working_memory=WorkingMemory(
                    step_count=(
                        len(preserved_timeline)
                        if preserved_timeline
                        else int(coordination is not None)
                    )
                ),
                trace_sink=InMemoryActionTraceSink(),
                final_text=(
                    "Web V2 页面探索已完成但会话关闭失败，证据摘要已保留"
                    if close_only_failure
                    else "Web V2 页面探索失败"
                ),
                error_message=str(failure),
                error_category=ErrorCategory.INFRASTRUCTURE,
                execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
                exploration_status="failed",
                evidence_gap=(
                    ("WEB_SESSION_CLOSE_FAILED",)
                    if close_only_failure
                    else (
                        "WEB_EVIDENCE_INCOMPLETE",
                        *(("WEB_SESSION_CLOSE_FAILED",) if close_failure else ()),
                    )
                ),
                evidence_index_id=(preserved_timeline[-1] if preserved_timeline else None),
                page_timeline=preserved_timeline,
                web_exploration_summary=preserved_summary,
            )

        if exploration is None or not exploration.page_timeline:
            return RunResult(
                termination_reason=TerminationReason.ERROR,
                working_memory=WorkingMemory(step_count=1),
                trace_sink=InMemoryActionTraceSink(),
                final_text="Web V2 页面探索缺少动作后证据",
                error_message="WEB_EVIDENCE_INCOMPLETE: 首跳没有动作后 Capture",
                error_category=ErrorCategory.INFRASTRUCTURE,
                execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
                exploration_status="failed",
                evidence_gap=("WEB_EVIDENCE_INCOMPLETE",),
            )
        evidence_index_id = exploration.page_timeline[-1]
        summary = _web_exploration_summary(
            exploration,
            adult_self_attestation=adult_self_attestation,
            preparation=preparation,
        )
        menu_total = int(summary.get("menu_total") or 0)
        if stage_gap_codes:
            summary["stage_gap_codes"] = list(stage_gap_codes)
        partial = bool(stage_gap_codes) or _web_exploration_is_partial(exploration, summary=summary)
        reportable_coverage_gap = (
            partial and record.task_config.get("execution_mode") == "full_inspection"
        )
        from services.api.app.application.web_exploration_progress import web_menu_progress

        progress = web_menu_progress(summary)
        terminal_count = progress["terminal"]
        failed_count = max(progress["failed"], len(exploration.failed_urls))
        return RunResult(
            termination_reason=(
                TerminationReason.INCOMPLETE
                if partial and not reportable_coverage_gap
                else TerminationReason.COMPLETED
            ),
            working_memory=WorkingMemory(step_count=len(exploration.page_timeline)),
            trace_sink=InMemoryActionTraceSink(),
            final_text=(
                "Web V2 桌面有界菜单探索与完整账本留存完成，"
                + (
                    f"共形成 {terminal_count} 个菜单终态，{progress['details']} 个详情采集成功，"
                    f"{progress['skipped']} 个因菜单额度跳过"
                    if menu_total
                    else f"共覆盖 {len(exploration.page_type_keys)} 个页面类型"
                )
                + (f"，另有 {failed_count} 个导航页面需复核" if partial else "")
            ),
            execution_outcome=(
                ExecutionOutcome.NEEDS_REVIEW if partial else ExecutionOutcome.SUCCESS
            ),
            exploration_status="partial_evidence" if partial else "evidence_ready",
            evidence_gap=tuple(
                dict.fromkeys(
                    (*stage_gap_codes, *(("WEB_PAGE_NAVIGATION_PARTIAL",) if partial else ()))
                )
            ),
            evidence_index_id=evidence_index_id,
            page_timeline=exploration.page_timeline,
            web_exploration_summary=summary,
        )


def build_web_formal_session_closer(
    settings: Settings,
) -> DurableWebFormalSessionCloser:
    """构建不依赖当前进程 Runtime 缓存的 Web 正式 Session 关闭器。"""

    return DurableWebFormalSessionCloser(
        runtime_factory=lambda: build_default_web_capture_runtime(
            session_owner=WEB_SURFACE_SESSION_OWNER,
            include_h5_exploration=False,
            h5_visual_coordinate_mode="off",
            logical_surface_enabled=False,
            logical_surface_shadow=False,
            logical_targets_enabled=False,
            web_desktop_policy_enabled=True,
            evidence_exit_gate=_build_payload_exit_gate(settings),
        )
    )


def build_web_workflow_handler(
    settings: Settings,
    worker_instance_id: str,
    *,
    publication_wakeup: WebInspectionPublicationWakeup | None = None,
) -> RuntimeBackedWebWorkflowHandler:
    """构建 Web 专用 Handler；settings 保留为显式组合根输入。"""

    formal_session_closer = build_web_formal_session_closer(settings)
    return RuntimeBackedWebWorkflowHandler(
        provider=PostgresWebRuntimeProvider(
            page_inspection_notification=build_default_web_page_inspection_notification(
                settings=settings,
                wakeup=publication_wakeup,
            ),
            formal_session_closer=formal_session_closer,
        ),
        executor=ProductionWebV2RuntimeExecutor(
            worker_instance_id=worker_instance_id,
        ),
        formal_session_closer=formal_session_closer,
    )


def _locked_web_runtime_config(record: AgentRunRecord) -> tuple[str, str]:
    execution_mode = record.task_config.get("execution_mode")
    notification_mode = record.task_config.get("web_inspection_notification_mode", "off")
    if execution_mode not in {"exploration_only", "full_inspection"}:
        raise WebRuntimeConfigurationError("WEB_EXECUTION_MODE_INVALID: Web Runtime 执行模式无效")
    if execution_mode == "full_inspection" and notification_mode != "enforced":
        raise WebRuntimeConfigurationError(
            "WEB_INSPECTION_NOTIFICATION_MODE_INVALID: 正式 Web 任务必须 enforced"
        )
    if execution_mode == "exploration_only" and notification_mode != "off":
        raise WebRuntimeConfigurationError(
            "WEB_INSPECTION_NOTIFICATION_MODE_INVALID: 仅探索 Web 任务必须 off"
        )
    target = record.task_config.get("_task_target")
    if not isinstance(target, Mapping) or target.get("platform") != "web":
        raise WebRuntimeConfigurationError("WEB_TARGET_INVALID: Run 未锁定 Web 目标")
    target_url = str(target.get("target_url") or "").strip()
    web = target.get("web")
    if not isinstance(web, Mapping):
        raise WebRuntimeConfigurationError("WEB_PROFILE_UNSUPPORTED: 缺少 Web 目标配置")
    profile_id = str(web.get("browser_profile_id") or "").strip()
    profile = WebBrowserProfileRegistry().resolve(profile_id)
    snapshot = record.task_config.get("browser_profile_snapshot")
    if not isinstance(snapshot, Mapping) or dict(snapshot) != profile.to_dict():
        raise WebRuntimeConfigurationError(
            "WEB_PROFILE_SNAPSHOT_MISMATCH: Browser Profile 快照与目标引用不一致"
        )
    return target_url, profile.profile_id


def _formal_web_notification_enabled(task_config: Mapping[str, object]) -> bool:
    return (
        task_config.get("execution_mode") == "full_inspection"
        and task_config.get("web_inspection_notification_mode") == "enforced"
    )


def _required_task_id(record: AgentRunRecord) -> str:
    if not isinstance(record.task_id, str) or not record.task_id.strip():
        raise WebRuntimeConfigurationError("WEB_TASK_ID_MISSING: Web Run 缺少 task_id")
    return record.task_id.strip()


def _adult_self_attestation(record: AgentRunRecord) -> AdultSelfAttestation | None:
    target = record.task_config.get("_task_target")
    web = target.get("web") if isinstance(target, Mapping) else None
    payload = web.get("adult_self_attestation") if isinstance(web, Mapping) else None
    if payload is None:
        return None
    try:
        return AdultSelfAttestation.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise WebRuntimeConfigurationError(f"WEB_ADULT_ATTESTATION_INVALID: {exc}") from exc


def _web_exploration_summary(
    exploration: object,
    *,
    adult_self_attestation: AdultSelfAttestation | None,
    preparation: WebHomePreparationResult | None,
) -> dict[str, object]:
    to_dict = getattr(exploration, "to_dict", None)
    if callable(to_dict):
        raw = to_dict()
        summary = dict(raw) if isinstance(raw, Mapping) else {}
    else:
        summary = {
            "site_id": str(getattr(exploration, "site_id", "") or ""),
            "catalog_version": str(getattr(exploration, "catalog_version", "") or ""),
            "menu_total": int(getattr(exploration, "menu_total", 0) or 0),
            "frontier_remaining": int(getattr(exploration, "frontier_remaining", 0) or 0),
            "status_counts": dict(getattr(exploration, "status_counts", {}) or {}),
            "page_timeline": list(getattr(exploration, "page_timeline", ()) or ()),
            "page_type_keys": list(getattr(exploration, "page_type_keys", ()) or ()),
        }
    summary["adult_self_attestation"] = (
        adult_self_attestation.to_dict() if adult_self_attestation is not None else None
    )
    if preparation is not None:
        summary["home_ready_capture_id"] = preparation.ready_capture_id
        age = preparation.age_gate_result
        if age is not None:
            summary["age_gate"] = {
                "entry_capture_id": getattr(age, "entry_capture_id", None),
                "gate_present": getattr(age, "gate_present", None),
                "clicked_candidate_id": getattr(age, "clicked_candidate_id", None),
                "after_capture_id": getattr(age, "after_capture_id", None),
                "gate_disappeared": getattr(age, "gate_disappeared", None),
            }
        readiness = preparation.readiness_result
        if readiness is not None:
            summary["home_readiness"] = {
                "status": getattr(readiness, "status", None),
                "dismissed_layers": list(getattr(readiness, "dismissed_layers", ()) or ()),
                "evidence_capture_ids": list(getattr(readiness, "evidence_capture_ids", ()) or ()),
            }
    return summary


def _web_exploration_is_partial(
    exploration: object,
    *,
    summary: Mapping[str, object],
) -> bool:
    """只允许完整 Web V2 Catalog 账本进入成功终态。

    ``status_counts`` 是展示摘要，不能作为成功真相。本门禁重新遍历持久化账本，
    同时核对首页、冻结菜单上限、单详情语义和最小动作血缘；缺字段或未知状态均
    失败关闭。运行时额外发现但未进入 Catalog 的导航只作为审计信息，不扩预算。
    """

    menu_total = int(summary.get("menu_total") or 0)
    if menu_total <= 0:
        return True
    if bool(getattr(exploration, "failed_urls", ())):
        return True
    menu_limit = int(summary.get("menu_limit") or 0)
    if not 1 <= menu_limit <= 1000:
        return True
    if int(summary.get("frontier_remaining") or 0) != 0:
        return True

    raw_ledger = summary.get("menu_ledger")
    if not isinstance(raw_ledger, list | tuple) or len(raw_ledger) != menu_total:
        return True
    if not all(isinstance(item, Mapping) for item in raw_ledger):
        return True
    ledger = [item for item in raw_ledger if isinstance(item, Mapping)]
    from services.api.app.application.web_discovery_audit import web_discovery_is_audit_only

    discoveries = summary.get("runtime_discovered_ledger", [])
    if not isinstance(discoveries, list | tuple) or any(
        not isinstance(item, Mapping) or item.get("source_kind") != "runtime_discovered"
        for item in discoveries
    ):
        return True
    ledger.extend(discoveries)
    actual_counts = Counter(str(item.get("status") or "") for item in ledger)
    raw_counts = summary.get("status_counts")
    if not isinstance(raw_counts, Mapping):
        return True
    for status in set(actual_counts) | set(str(key) for key in raw_counts):
        if status == "runtime_discovered_unreviewed" and not discoveries:
            continue
        if actual_counts.get(status, 0) != int(raw_counts.get(status) or 0):
            return True

    if actual_counts.get("home_baseline", 0) != 1:
        return True
    ledger = [item for item in ledger if not web_discovery_is_audit_only(item)]
    execution_counts = Counter(str(item.get("status") or "") for item in ledger)
    success_statuses = {
        "home_baseline",
        "menu_page_captured",
        "detail_captured",
        "logical_target_reused",
        "cross_origin_blocked",
        "menu_limit_skipped",
    }
    if any(status not in success_statuses for status in execution_counts):
        return True

    executed = sum(int(item.get("menu_action_count") or 0) for item in ledger)
    if executed > menu_limit or executed != int(summary.get("executed_same_origin_count") or 0):
        return True
    skipped = actual_counts.get("menu_limit_skipped", 0)
    if skipped != int(summary.get("menu_limit_skipped_count") or 0):
        return True

    normalization_version = str(summary.get("normalization_version") or "")
    return any(
        not _web_success_ledger_entry_is_valid(
            item,
            normalization_version=normalization_version,
        )
        for item in ledger
    )


def _web_success_ledger_entry_is_valid(
    item: Mapping[str, object],
    *,
    normalization_version: str = "web-page-type-assessment.v3",
) -> bool:
    from services.api.app.application.web_home_baseline_validation import (
        persisted_home_baseline_has_valid_shape,
    )
    from services.api.app.application.web_submenu_expansion import (
        persisted_menu_page_is_submenu_expansion,
        persisted_ordinary_menu_page_has_valid_shape,
        persisted_submenu_expansion_has_valid_shape,
    )

    status = str(item.get("status") or "")
    menu_count = int(item.get("menu_action_count") or 0)
    detail_count = int(item.get("detail_action_count") or 0)
    if menu_count not in {0, 1} or detail_count not in {0, 1}:
        return False

    if status == "home_baseline":
        return persisted_home_baseline_has_valid_shape(
            item,
            normalization_version=normalization_version,
        )

    if status in {"menu_limit_skipped", "cross_origin_blocked"}:
        return (
            menu_count == 0
            and detail_count == 0
            and _all_absent(
                item,
                "menu_intent_id",
                "menu_action_execution_id",
                "menu_probe_capture_id",
                "menu_ready_capture_id",
                "detail_intent_id",
                "detail_action_execution_id",
                "detail_probe_capture_id",
                "detail_ready_capture_id",
            )
        )

    if status == "logical_target_reused":
        return (
            _all_present(item, "reused_capture_id", "reused_logical_page_id")
            and menu_count in {0, 1}
            and detail_count in {0, 1}
            and (menu_count == 1 or detail_count == 0)
            and (
                menu_count == 0
                or _all_present(
                    item,
                    "menu_action_execution_id",
                    "menu_probe_capture_id",
                    "menu_ready_capture_id",
                )
            )
            and (
                detail_count == 0
                or _all_present(
                    item,
                    "detail_action_execution_id",
                    "detail_probe_capture_id",
                    "detail_ready_capture_id",
                )
            )
        )
    if status == "menu_page_captured":
        if persisted_menu_page_is_submenu_expansion(item):
            return item.get(
                "submenu_relations_verified"
            ) is True and persisted_submenu_expansion_has_valid_shape(item)
        return persisted_ordinary_menu_page_has_valid_shape(item)
    if status != "detail_captured" or menu_count != 1:
        return False
    if not _all_present(
        item,
        "menu_intent_id",
        "menu_action_execution_id",
        "menu_probe_capture_id",
        "menu_ready_capture_id",
    ):
        return False
    if item.get("menu_probe_capture_id") == item.get("menu_ready_capture_id"):
        return False
    menu_page_type = str(item.get("menu_page_type") or "")
    if menu_page_type in WEB_DETAIL_PAGE_TYPES:
        detail_ready = str(item.get("detail_ready_capture_id") or "")
        menu_ready = str(item.get("menu_ready_capture_id") or "")
        detail_page_type = str(item.get("detail_page_type") or "")
        return (
            detail_count == 0
            and _all_absent(
                item,
                "detail_intent_id",
                "detail_action_execution_id",
                "detail_probe_capture_id",
            )
            and (not detail_ready or detail_ready == menu_ready)
            and (not detail_page_type or detail_page_type == menu_page_type)
        )
    if menu_page_type not in WEB_LIST_PAGE_TYPES or detail_count != 1:
        return False
    return (
        _all_present(
            item,
            "detail_intent_id",
            "detail_action_execution_id",
            "detail_probe_capture_id",
            "detail_ready_capture_id",
        )
        and item.get("detail_probe_capture_id") != item.get("detail_ready_capture_id")
        and str(item.get("detail_page_type") or "") in WEB_DETAIL_PAGE_TYPES
    )


def _all_present(item: Mapping[str, object], *keys: str) -> bool:
    return all(str(item.get(key) or "").strip() for key in keys)


def _all_absent(item: Mapping[str, object], *keys: str) -> bool:
    return all(not str(item.get(key) or "").strip() for key in keys)


def _max_duration_seconds(record: AgentRunRecord, settings: Settings) -> float:
    value = record.loop_guard.get("max_duration_seconds")
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return float(settings.agent_runtime.max_duration_seconds)


def _exception_type(exc: Exception) -> str:
    normalized = "".join(
        character if character.isalnum() or character in {"_", "."} else "_"
        for character in type(exc).__name__
    )
    return normalized[:128] or "Exception"


def _build_payload_exit_gate(settings):
    from services.api.app.application.web_capture_preservation import build_web_payload_exit_gate

    return build_web_payload_exit_gate(settings=settings)


__all__ = [
    "PostgresWebRuntimeProvider",
    "ProductionWebV2RuntimeExecutor",
    "WebRuntimeConfigurationError",
    "WebV2Runtime",
    "build_web_formal_session_closer",
    "build_web_workflow_handler",
]
