"""Catalog 驱动的 Web 单站有界菜单 Frontier 与完整终态账本。"""

from __future__ import annotations

import inspect
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from services.api.app.application.web_desktop_navigation import (
    WebNavigationCandidate,
    canonical_navigation_identity,
    catalog_navigation_url_matches,
    isolate_navigation_candidate_identity,
    normalized_menu_path,
)
from services.api.app.application.web_detail_selection_diagnostics import (
    bounded_detail_selection_diagnostic,
)
from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerConflictError,
    WebExplorationBudgetLedgerPort,
    WebMenuClaimRejection,
    WebMenuLedgerTerminalRecord,
)
from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
from services.api.app.application.web_interaction_precision import (
    WebInteractionOutcome,
    WebInteractionPlan,
)
from services.api.app.application.web_logical_page import canonical_web_logical_page_url
from services.api.app.application.web_runtime_discovery import (
    WebRuntimeMenuEntry,
    discovery_entry_id,
    discovery_safety_reason,
)
from services.api.app.application.web_submenu_expansion import (
    is_proven_submenu_expansion,
    persisted_submenu_expansion_has_valid_shape,
)
from services.api.app.domain.exploration import (
    WEB_DETAIL_PAGE_TYPES,
    WEB_LIST_PAGE_TYPES,
    WebMenuTerminalStatus,
)
from services.api.app.domain.exploration.web_discovery_policy import WebDiscoveryPolicy
from services.api.app.domain.page import (
    WebCatalogStatus,
    WebMenuDecision,
    WebSiteMenuCatalog,
    WebSiteMenuEntry,
)
from services.api.app.domain.page.web_runtime_navigation_context import RuntimeWebNavigationContext

WEB_MENU_COLLECTION_LIMIT: None = None
_WEB_BUSINESS_PAGE_TYPES = WEB_LIST_PAGE_TYPES | WEB_DETAIL_PAGE_TYPES | {"home"}


@dataclass(frozen=True, kw_only=True)
class WebMenuCollectionPlan:
    """按逻辑 URL 生成一次性采集队列，别名仍保留在账本。"""

    selected_entries: tuple[WebSiteMenuEntry, ...]
    skipped_entries: tuple[WebSiteMenuEntry, ...]
    alias_of: dict[str, str]


class WebNavigationCandidateSource(Protocol):
    def navigation_candidates(
        self,
        session_id: str,
    ) -> tuple[WebNavigationCandidate, ...]: ...


@dataclass(frozen=True, kw_only=True)
class WebMenuVisitResult:
    """一个白名单菜单执行后交回账本的证据摘要。"""

    final_capture_id: str | None
    page_type: str | None
    status: WebMenuTerminalStatus = WebMenuTerminalStatus.MENU_PAGE_CAPTURED
    detail_capture_id: str | None = None
    error_code: str | None = None
    menu_probe_capture_id: str | None = None
    menu_ready_capture_id: str | None = None
    menu_page_type: str | None = None
    menu_readiness_evidence_capture_ids: tuple[str, ...] = ()
    menu_dismissed_layers: tuple[str, ...] = ()
    detail_candidate_ids: tuple[str, ...] = ()
    selected_detail_candidate_id: str | None = None
    detail_selection_confidence: float | None = None
    detail_selection_reason: str | None = None
    detail_selection_diagnostic: Mapping[str, object] | None = None
    detail_probe_capture_id: str | None = None
    detail_ready_capture_id: str | None = None
    detail_page_type: str | None = None
    detail_readiness_evidence_capture_ids: tuple[str, ...] = ()
    detail_dismissed_layers: tuple[str, ...] = ()
    diagnostic_type: str | None = None
    diagnostic_message: str | None = None
    menu_action_count: int = 0
    menu_intent_id: str | None = None
    menu_action_execution_id: str | None = None
    detail_action_count: int = 0
    detail_intent_id: str | None = None
    detail_action_execution_id: str | None = None
    menu_interaction_outcome: WebInteractionOutcome | None = None
    menu_interaction_plan: WebInteractionPlan | None = None
    menu_before_url: str | None = None
    menu_observed_target_url: str | None = None
    menu_before_content_identity: str | None = None
    menu_receipt_verified: bool = False
    reused_capture_id: str | None = None
    reused_logical_page_id: str | None = None
    detail_interaction_outcome: WebInteractionOutcome | None = None
    detail_scroll_interaction_outcomes: tuple[WebInteractionOutcome, ...] = ()
    menu_dismissal_interaction_outcomes: tuple[WebInteractionOutcome, ...] = ()
    detail_dismissal_interaction_outcomes: tuple[WebInteractionOutcome, ...] = ()
    primary_content_diagnostics: tuple[dict[str, object], ...] = ()
    navigation_current_capture_id: str | None = None
    navigation_handoff_error: str | None = None
    ad_landing_evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_action_count(self.menu_action_count, "menu_action_count")
        _require_detail_action_count(self.detail_action_count)


class WebMenuPageVisitor(Protocol):
    async def visit(
        self,
        *,
        command: RunWebWorkflowCommand,
        entry: WebSiteMenuEntry,
        candidate: WebNavigationCandidate,
        source_capture_id: str,
        menu_intent_id: str,
    ) -> WebMenuVisitResult: ...


@dataclass(frozen=True, kw_only=True)
class WebMenuLedgerEntry:
    entry_id: str
    menu_path: tuple[str, ...]
    canonical_url: str
    status: WebMenuTerminalStatus
    source_capture_id: str
    final_capture_id: str | None
    page_type: str | None
    detail_capture_id: str | None
    error_code: str | None
    decision: str | None = None
    execution_slot: int | None = None
    alias_of: str | None = None
    menu_probe_capture_id: str | None = None
    menu_ready_capture_id: str | None = None
    menu_page_type: str | None = None
    menu_readiness_evidence_capture_ids: tuple[str, ...] = ()
    menu_dismissed_layers: tuple[str, ...] = ()
    detail_candidate_ids: tuple[str, ...] = ()
    selected_detail_candidate_id: str | None = None
    detail_selection_confidence: float | None = None
    detail_selection_reason: str | None = None
    detail_selection_diagnostic: Mapping[str, object] | None = None
    detail_probe_capture_id: str | None = None
    detail_ready_capture_id: str | None = None
    detail_page_type: str | None = None
    detail_readiness_evidence_capture_ids: tuple[str, ...] = ()
    detail_dismissed_layers: tuple[str, ...] = ()
    diagnostic_type: str | None = None
    diagnostic_message: str | None = None
    menu_action_count: int = 0
    menu_intent_id: str | None = None
    menu_action_execution_id: str | None = None
    detail_action_count: int = 0
    detail_intent_id: str | None = None
    detail_action_execution_id: str | None = None
    reused_capture_id: str | None = None
    reused_logical_page_id: str | None = None
    menu_interaction_outcome: Mapping[str, object] = field(default_factory=dict)
    parent_menu_entry_id: str | None = None
    parent_logical_target_identity: str | None = None
    menu_depth: int | None = None
    exploration_path: tuple[str, ...] = ()
    logical_target_identity: str | None = None
    submenu_relations_verified: bool = False
    ad_landing_evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_action_count(self.menu_action_count, "menu_action_count")
        _require_detail_action_count(self.detail_action_count)
        lineage_present = bool(
            self.menu_depth is not None
            or self.exploration_path
            or self.logical_target_identity is not None
            or self.parent_menu_entry_id is not None
        )
        if lineage_present and (
            isinstance(self.menu_depth, bool)
            or not isinstance(self.menu_depth, int)
            or self.menu_depth < 1
            or not self.exploration_path
            or not isinstance(self.logical_target_identity, str)
            or not self.logical_target_identity.strip()
            or len(self.logical_target_identity) != 64
            or any(
                character not in "0123456789abcdef" for character in self.logical_target_identity
            )
            or self.entry_id != "discovery:" + self.logical_target_identity
            or any(not isinstance(item, str) or not item.strip() for item in self.exploration_path)
            or not self.menu_path
            or self.exploration_path[-1] != self.menu_path[-1]
        ):
            raise ValueError("WEB_MENU_LINEAGE_INVALID: 持久化菜单谱系不完整")

    def to_dict(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "menu_path": list(self.menu_path),
            "canonical_url": self.canonical_url,
            "status": self.status.value,
            "decision": self.decision,
            "execution_slot": self.execution_slot,
            "source_capture_id": self.source_capture_id,
            "final_capture_id": self.final_capture_id,
            "page_type": self.page_type,
            "detail_capture_id": self.detail_capture_id,
            "error_code": self.error_code,
            "alias_of": self.alias_of,
            "menu_probe_capture_id": self.menu_probe_capture_id,
            "menu_ready_capture_id": self.menu_ready_capture_id,
            "menu_page_type": self.menu_page_type,
            "menu_readiness_evidence_capture_ids": list(self.menu_readiness_evidence_capture_ids),
            "menu_dismissed_layers": list(self.menu_dismissed_layers),
            "detail_candidate_ids": list(self.detail_candidate_ids),
            "selected_detail_candidate_id": self.selected_detail_candidate_id,
            "detail_selection_confidence": self.detail_selection_confidence,
            "detail_selection_reason": self.detail_selection_reason,
            **(
                {"detail_selection_diagnostic": bounded_detail_selection_diagnostic(
                    self.detail_selection_diagnostic
                )}
                if self.detail_selection_diagnostic else {}
            ),
            "detail_probe_capture_id": self.detail_probe_capture_id,
            "detail_ready_capture_id": self.detail_ready_capture_id,
            "detail_page_type": self.detail_page_type,
            "detail_readiness_evidence_capture_ids": list(
                self.detail_readiness_evidence_capture_ids
            ),
            "detail_dismissed_layers": list(self.detail_dismissed_layers),
            "diagnostic_type": self.diagnostic_type,
            "diagnostic_message": self.diagnostic_message,
            "menu_action_count": self.menu_action_count,
            "menu_intent_id": self.menu_intent_id,
            "menu_action_execution_id": self.menu_action_execution_id,
            "detail_action_count": self.detail_action_count,
            "detail_intent_id": self.detail_intent_id,
            "detail_action_execution_id": self.detail_action_execution_id,
            "reused_capture_id": self.reused_capture_id,
            "reused_logical_page_id": self.reused_logical_page_id,
            "menu_interaction_outcome": dict(self.menu_interaction_outcome),
            "parent_menu_entry_id": self.parent_menu_entry_id,
            "parent_logical_target_identity": self.parent_logical_target_identity,
            "menu_depth": self.menu_depth,
            "exploration_path": list(self.exploration_path),
            "logical_target_identity": self.logical_target_identity,
            "submenu_relations_verified": self.submenu_relations_verified,
            "ad_landing_evidence": dict(self.ad_landing_evidence),
        }


@dataclass(frozen=True, kw_only=True)
class WebRuntimeDiscoveredLedgerEntry:
    """运行时观察到但未进入 Catalog 的只读导航记录。"""

    entry_id: str
    menu_path: tuple[str, ...]
    canonical_url: str | None
    menu_position: int
    source_capture_id: str
    source_kind: str = "anchor"
    target_hint: str = ""
    actionability: str = "actionable"
    risk_level: str = "low"
    status: WebMenuTerminalStatus = WebMenuTerminalStatus.RUNTIME_DISCOVERED_UNREVIEWED
    error_code: str = "WEB_MENU_RUNTIME_DISCOVERED_UNREVIEWED"
    candidate_id: str | None = None
    candidate_source_kind: str | None = None
    execution: WebMenuLedgerEntry | None = None
    # 原始账本事实仅用于审计投影，不把零派发发现变成已执行记录。
    persisted_ledger_entry: WebMenuLedgerEntry | None = field(default=None, repr=False)
    decision: str | None = None
    execution_slot: int | None = None
    persisted_status: str | None = None
    parent_menu_entry_id: str | None = None
    parent_logical_target_identity: str | None = None
    menu_depth: int | None = None
    exploration_path: tuple[str, ...] = ()
    logical_target_identity: str | None = None
    candidate_manifest: Mapping[str, object] = field(default_factory=dict, repr=False)

    def restored_navigation_candidate(self) -> WebNavigationCandidate | None:
        """从受验证账本投影恢复候选；manifest 与持久谱系不一致即拒绝。"""
        raw = dict(self.candidate_manifest)
        for key in ("menu_path", "exploration_path", "navigation_evidence"):
            value = raw.get(key)
            if isinstance(value, list):
                raw[key] = tuple(value)
        try:
            candidate = WebNavigationCandidate(**raw)
        except (TypeError, ValueError):
            return None
        if (
            self.entry_id != discovery_entry_id(candidate)
            or candidate.parent_menu_entry_id != self.parent_menu_entry_id
            or candidate.parent_logical_target_identity != self.parent_logical_target_identity
            or candidate.menu_depth != self.menu_depth
            or candidate.exploration_path != self.exploration_path
            or candidate.logical_target_identity != self.logical_target_identity
        ):
            return None
        return candidate

    def to_dict(self) -> dict[str, object]:
        ledger_entry = self.execution if self.execution is not None else self.persisted_ledger_entry
        return {
            **(ledger_entry.to_dict() if ledger_entry is not None else {}),
            "entry_id": self.entry_id,
            "menu_path": list(self.menu_path),
            "canonical_url": self.canonical_url,
            "menu_position": self.menu_position,
            "source_capture_id": self.source_capture_id,
            "source_kind": self.source_kind,
            "target_hint": self.target_hint,
            "actionability": self.actionability,
            "risk_level": self.risk_level,
            "status": self.status.value,
            "error_code": self.error_code,
            "candidate_id": self.candidate_id,
            "candidate_source_kind": self.candidate_source_kind,
            "decision": self.decision,
            "execution_slot": self.execution_slot,
            "parent_menu_entry_id": self.parent_menu_entry_id,
            "parent_logical_target_identity": self.parent_logical_target_identity,
            "menu_depth": self.menu_depth,
            "exploration_path": list(self.exploration_path),
            "logical_target_identity": self.logical_target_identity,
        }


@dataclass(frozen=True, kw_only=True)
class WebMenuExplorationSummary:
    page_timeline: tuple[str, ...]
    page_type_keys: tuple[str, ...]
    navigation_current_capture_id: str | None = None
    site_id: str = "unregistered-home-only"
    normalization_version: str = "web-page-type-assessment.v3"
    catalog_version: str = "none"
    catalog_sha256: str = "none"
    menu_total: int = 1
    menu_limit: int | None = WEB_MENU_COLLECTION_LIMIT
    executed_same_origin_count: int = 0
    menu_limit_skipped_count: int = 0
    frontier_remaining: int = 0
    menu_ledger: tuple[WebMenuLedgerEntry, ...] = ()
    runtime_discovered_ledger: tuple[WebRuntimeDiscoveredLedgerEntry, ...] = ()
    status_counts: dict[str, int] = field(default_factory=dict)

    @property
    def runtime_discovered_count(self) -> int:
        return len(self.runtime_discovered_ledger)

    @property
    def failed_urls(self) -> tuple[str, ...]:
        failed_statuses = {
            WebMenuTerminalStatus.POPUP_BLOCKED,
            WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED,
            WebMenuTerminalStatus.UNSAFE_NAVIGATION,
            WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED,
            WebMenuTerminalStatus.DETAIL_SELECTION_UNTRUSTED,
            WebMenuTerminalStatus.DETAIL_PAGE_TYPE_MISMATCH,
            WebMenuTerminalStatus.NAVIGATION_FAILED,
            WebMenuTerminalStatus.ALLOWLIST_DRIFTED,
        }
        return tuple(
            item.canonical_url for item in self.menu_ledger if item.status in failed_statuses
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "site_id": self.site_id,
            "normalization_version": self.normalization_version,
            "catalog_version": self.catalog_version,
            "catalog_sha256": self.catalog_sha256,
            "menu_total": self.menu_total,
            "menu_limit": self.menu_limit,
            "executed_same_origin_count": self.executed_same_origin_count,
            "menu_limit_skipped_count": self.menu_limit_skipped_count,
            "frontier_remaining": self.frontier_remaining,
            "menu_ledger": [item.to_dict() for item in self.menu_ledger],
            "runtime_discovered_count": self.runtime_discovered_count,
            "runtime_discovered_ledger": [
                item.to_dict() for item in self.runtime_discovered_ledger
            ],
            "status_counts": dict(self.status_counts),
            "page_timeline": list(self.page_timeline),
            "page_type_keys": list(self.page_type_keys),
        }


@dataclass(frozen=True, kw_only=True)
class WebMenuReconciliation:
    matched_candidates: dict[str, WebNavigationCandidate]
    runtime_unreviewed: tuple[WebNavigationCandidate, ...]


def reconcile_site_menu(
    site: WebSiteMenuCatalog,
    runtime_candidates: tuple[WebNavigationCandidate, ...],
) -> WebMenuReconciliation:
    """优先按完整路径对账；标签漂移时只接受双侧唯一的可信 href。"""

    unused = [isolate_navigation_candidate_identity(item) for item in runtime_candidates]
    matched: dict[str, WebNavigationCandidate] = {}
    for entry in site.entries:
        if (
            entry.decision is not WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
            or entry.status is WebCatalogStatus.DEPRECATED
            or not entry.collection_eligibility.eligible
        ):
            continue
        expected_path = normalized_menu_path(entry.menu_path)
        match_index = next(
            (
                index
                for index, candidate in enumerate(unused)
                if _candidate_is_executable(candidate)
                and catalog_navigation_url_matches(
                    entry.canonical_url,
                    candidate.url,
                )
                and normalized_menu_path(candidate.menu_path) == expected_path
            ),
            None,
        )
        if match_index is None:
            runtime_url_matches = [
                index
                for index, candidate in enumerate(unused)
                if _candidate_is_executable(candidate)
                and catalog_navigation_url_matches(
                    entry.canonical_url,
                    candidate.url,
                )
            ]
            catalog_url_matches = list(site.advisory_entries_for_url(entry.canonical_url))
            if len(runtime_url_matches) != 1 or len(catalog_url_matches) != 1:
                continue
            match_index = runtime_url_matches[0]
        matched[entry.entry_id] = replace(
            unused.pop(match_index),
            allowlist_match=True,
        )
    return WebMenuReconciliation(
        matched_candidates=matched,
        runtime_unreviewed=tuple(unused),
    )


class WebMenuExplorer:
    """按 Catalog 顺序执行冻结预算内的同源菜单，并闭合完整菜单账本。"""

    def __init__(
        self,
        *,
        navigation_context: RuntimeWebNavigationContext,
        candidate_source: WebNavigationCandidateSource,
        page_visitor: WebMenuPageVisitor,
        budget_ledger: WebExplorationBudgetLedgerPort | None = None,
        logical_target_repository: object | None = None,
        discovery_policy: WebDiscoveryPolicy | None = None,
        required_page_types: tuple[str, ...] = (),
    ) -> None:
        self._navigation_context = navigation_context
        self._candidate_source = candidate_source
        self._page_visitor = page_visitor
        self._budget_ledger = budget_ledger
        self._logical_target_repository = logical_target_repository
        self.discovery_policy = discovery_policy or navigation_context.discovery_policy
        self.required_page_types = required_page_types

    async def explore(
        self,
        command: RunWebWorkflowCommand,
        *,
        initial_capture_id: str,
        initial_page_type: str | None = None,
        initial_navigation_capture_id: str | None = None,
    ) -> WebMenuExplorationSummary:
        if not isinstance(command, RunWebWorkflowCommand):
            raise TypeError("command 必须为 RunWebWorkflowCommand")
        if not isinstance(initial_capture_id, str) or not initial_capture_id.strip():
            raise ValueError("initial_capture_id 不能为空")
        if initial_page_type is not None and initial_page_type not in _WEB_BUSINESS_PAGE_TYPES:
            raise ValueError("initial_page_type 必须为 Web 五类业务页型之一")
        context = self._navigation_context.resolve_site_context(command.target_url)
        site = context.site
        candidate_pool: dict[str, WebNavigationCandidate] = {}

        async def refresh_candidates() -> None:
            observed = self._candidate_source.navigation_candidates(command.session_id)
            if inspect.isawaitable(observed):
                observed = await observed
            for candidate in observed:
                identity = candidate.logical_target_identity
                previous = candidate_pool.get(identity)
                candidate_pool[identity] = replace(
                    candidate,
                    discovery_capture_id=(
                        candidate.discovery_capture_id
                        or (previous.discovery_capture_id if previous is not None else None)
                        or candidate.source_capture_id
                    ),
                )

        menu_limit = WEB_MENU_COLLECTION_LIMIT
        persisted_summary = None
        if self._budget_ledger is not None:
            persisted_summary = await self._budget_ledger.read_summary(
                agent_run_id=command.agent_run_id,
                site_id=site.site_id,
            )
            menu_limit = persisted_summary.menu_limit
            if menu_limit is not None and (
                type(menu_limit) is not int or not 1 <= menu_limit <= 1000
            ):
                raise WebExplorationBudgetLedgerConflictError("Web 持久预算的菜单上限不受支持")
            if self.discovery_policy.enabled:
                restore_persisted_navigation_state(
                    self._candidate_source,
                    persisted_summary,
                    session_id=command.session_id,
                )
        await refresh_candidates()
        collection_plan = plan_web_menu_collection(
            site,
            menu_limit=menu_limit,
            discovery_policy=self.discovery_policy,
            required_page_types=self.required_page_types,
        )
        pending_entries = list(collection_plan.selected_entries)
        discovery_seen: set[str] = set()
        discovery_reasons: dict[str, str] = {}
        if persisted_summary is not None and self.discovery_policy.enabled:
            # 终态已经结算；未知 reservation 保持阻断，不重新发浏览器动作。
            if any(
                row.execution is not None
                and row.execution.menu_action_count
                and not _runtime_discovery_execution_is_complete(row.execution)
                for row in persisted_summary.runtime_discovered_ledger
            ):
                return await self._read_persistent_summary(command, site)
            previous_entries = {item.entry_id: item for item in persisted_summary.menu_ledger}
            if any(
                item.menu_action_count and item.error_code == "WEB_BUDGET_RESERVED_NEEDS_REVIEW"
                for item in (
                    *persisted_summary.menu_ledger,
                    *(
                        row.execution
                        for row in persisted_summary.runtime_discovered_ledger
                        if row.execution is not None
                    ),
                )
            ):
                return await self._read_persistent_summary(command, site)
            pending_entries = [
                entry
                for entry in pending_entries
                if (
                    previous_entries[entry.entry_id].error_code
                    == "WEB_BUDGET_RESERVED_NEEDS_REVIEW"
                    and previous_entries[entry.entry_id].menu_action_count == 0
                )
            ]
            discovery_seen.update(
                item.entry_id
                for item in persisted_summary.runtime_discovered_ledger
                if item.execution is not None and item.execution.menu_action_count
            )
        menu_limit_skipped_entries = (
            list(collection_plan.skipped_entries) if menu_limit is not None else []
        )
        selected_entry_ids = {entry.entry_id for entry in collection_plan.selected_entries}
        frontier_remaining = len(pending_entries)
        executed_same_origin_count = 0
        current_capture_id = initial_navigation_capture_id or initial_capture_id
        authoritative_current = initial_navigation_capture_id is not None
        page_timeline = [initial_capture_id]
        page_type_keys = [initial_page_type] if initial_page_type is not None else []
        ledger_by_entry_id: dict[str, WebMenuLedgerEntry] = {}
        site_human_verification_blocked = False
        discovery_outcome_unproven = False

        async def persist_discoveries(reconciliation):
            for observed in reconciliation.runtime_unreviewed:
                if _matches_non_executable_catalog_entry(observed, site):
                    continue
                identity = discovery_entry_id(observed)
                reason = discovery_reasons.get(identity) or (
                    "WEB_DISCOVERY_DISABLED"
                    if not self.discovery_policy.enabled
                    else discovery_safety_reason(observed, site_origin=site.origin)
                    or "WEB_DISCOVERY_PENDING"
                )
                if self._budget_ledger is not None:
                    await self._budget_ledger.record_discovery(
                        command=command,
                        site=site,
                        candidate=observed,
                        source_capture_id=observed.discovery_capture_id
                        or observed.source_capture_id
                        or initial_capture_id,
                        error_code=reason,
                    )

        for entry in site.entries:
            if entry.decision is WebMenuDecision.HOME_BASELINE:
                ledger_by_entry_id[entry.entry_id] = _ledger(
                    entry,
                    status=(
                        WebMenuTerminalStatus.HOME_BASELINE
                        if initial_page_type is not None
                        else WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED
                    ),
                    source_capture_id=initial_capture_id,
                    final_capture_id=initial_capture_id,
                    page_type=initial_page_type,
                    error_code=(
                        None if initial_page_type is not None else "WEB_PAGE_TYPE_UNCLASSIFIED"
                    ),
                )
                continue
            if entry.decision is WebMenuDecision.BLOCKED_CROSS_ORIGIN:
                ledger_by_entry_id[entry.entry_id] = _ledger(
                    entry,
                    status=WebMenuTerminalStatus.CROSS_ORIGIN_BLOCKED,
                    source_capture_id=initial_capture_id,
                    error_code="WEB_MENU_CROSS_ORIGIN_BLOCKED",
                )
                continue
            if (
                entry.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
                and entry.entry_id not in selected_entry_ids
            ):
                if entry.entry_id in collection_plan.alias_of:
                    error_code = "WEB_MENU_LOGICAL_ALIAS"
                    alias_of = collection_plan.alias_of[entry.entry_id]
                elif entry.status is WebCatalogStatus.DEPRECATED:
                    error_code = "WEB_MENU_CATALOG_DEPRECATED"
                    alias_of = None
                elif not entry.collection_eligibility.eligible:
                    error_code = "WEB_MENU_COLLECTION_INELIGIBLE"
                    alias_of = None
                else:
                    error_code = "WEB_MENU_LIMIT_SKIPPED"
                    alias_of = None
                ledger_by_entry_id[entry.entry_id] = _ledger(
                    entry,
                    status=WebMenuTerminalStatus.MENU_LIMIT_SKIPPED,
                    source_capture_id=initial_capture_id,
                    error_code=error_code,
                    alias_of=alias_of,
                )
        while pending_entries or self.discovery_policy.enabled:
            await refresh_candidates()
            reconciliation = reconcile_site_menu(site, tuple(candidate_pool.values()))
            await persist_discoveries(reconciliation)
            entry = next(
                (
                    pending
                    for pending in pending_entries
                    if pending.entry_id in reconciliation.matched_candidates
                ),
                None,
            )
            if entry is None:
                candidate = None
                for discovered in reconciliation.runtime_unreviewed:
                    if _matches_non_executable_catalog_entry(discovered, site):
                        continue
                    identity = discovery_entry_id(discovered)
                    if identity in discovery_seen:
                        continue
                    discovery_seen.add(identity)
                    reason = discovery_safety_reason(discovered, site_origin=site.origin)
                    if reason is None and not self.discovery_policy.enabled:
                        reason = "WEB_DISCOVERY_DISABLED"
                    if reason is None and self._budget_ledger is None:
                        reason = "WEB_DISCOVERY_PERSISTENCE_REQUIRED"
                    if reason is None and site_human_verification_blocked:
                        reason = "WEB_SITE_HUMAN_VERIFICATION_BLOCKED"
                    if reason is None and discovery_outcome_unproven:
                        reason = "WEB_DISCOVERY_UNKNOWN_OUTCOME_BLOCKED"
                    if (
                        reason is None
                        and (
                            command.deadline - datetime.now(command.deadline.tzinfo)
                        ).total_seconds()
                        <= 15
                    ):
                        reason = "WEB_DISCOVERY_CLOSEOUT_RESERVED"
                    # DOM href 在发现时已经冻结并持久化；当前页变化只改变下一次 GOTO 的
                    # 动作源 Capture，不应令同一 Web Run 内的原始导航证据失效。
                    if reason is not None:
                        discovery_reasons[identity] = reason
                        continue
                    candidate = discovered
                    entry = WebRuntimeMenuEntry.from_candidate(candidate)
                    pending_entries.append(entry)
                    frontier_remaining += 1
                    break
                if entry is None:
                    break
            else:
                candidate = reconciliation.matched_candidates[entry.entry_id]
            source_capture_id = (
                current_capture_id
                if authoritative_current
                else (candidate.source_capture_id or current_capture_id)
            )
            if self._logical_target_repository is not None and entry.canonical_url:
                known = await self._logical_target_repository.find(
                    task_id=command.task_id,
                    agent_run_id=command.agent_run_id,
                    session_id=command.session_id,
                    source_capture_id=source_capture_id,
                    url=entry.canonical_url,
                )
                if known is not None:
                    if known["state"] != "materialized":
                        raise ValueError("WEB_LOGICAL_TARGET_INCOMPLETE")
                    await self._logical_target_repository.record_menu_reuse(
                        command=command,
                        site_id=site.site_id,
                        entry=entry,
                        source_capture_id=source_capture_id,
                        target=known,
                    )
                    pending_entries.remove(entry)
                    frontier_remaining -= 1
                    continue
            reservation = None
            if self._budget_ledger is not None:
                try:
                    reservation = await self._budget_ledger.claim_menu(
                        command=command,
                        entry=entry,
                        source_capture_id=source_capture_id,
                    )
                except Exception:  # noqa: BLE001 - reservation 漂移必须立即失败关闭
                    return await self._read_persistent_summary(command, site)
                if isinstance(reservation, WebMenuClaimRejection):
                    # 目录已在作用域/预算行锁内结算，发现保留安全原因并同步持久收口。
                    for observed in reconciliation.runtime_unreviewed:
                        discovery_reasons[discovery_entry_id(observed)] = (
                            discovery_safety_reason(observed, site_origin=site.origin)
                            or "WEB_DISCOVERY_CLOSEOUT_RESERVED"
                        )
                    await persist_discoveries(reconciliation)
                    return await self._read_persistent_summary(command, site)
                if reservation is None or reservation.already_reserved:
                    if isinstance(entry, WebRuntimeMenuEntry) and reservation is None:
                        rejected = await self._read_persistent_summary(command, site)
                        discovery_reasons[entry.entry_id] = next(
                            (
                                item.error_code
                                for item in rejected.runtime_discovered_ledger
                                if item.entry_id == entry.entry_id and item.error_code
                            ),
                            "WEB_DISCOVERY_BUDGET_EXHAUSTED",
                        )
                        pending_entries.remove(entry)
                        frontier_remaining -= 1
                        continue
                    return await self._read_persistent_summary(command, site)
            executed_same_origin_count += 1
            try:
                set_parent = getattr(self._candidate_source, "set_navigation_parent", None)
                if callable(set_parent):
                    parent_candidate = candidate
                    if not isinstance(entry, WebRuntimeMenuEntry):
                        parent_candidate = replace(
                            candidate,
                            label=entry.menu_path[-1],
                            menu_path=entry.menu_path,
                            parent_menu_entry_id=None,
                            parent_logical_target_identity=None,
                            menu_depth=1,
                            exploration_path=entry.menu_path,
                        )
                    set_parent(
                        command.session_id,
                        parent_candidate,
                        parent_menu_entry_id=entry.entry_id,
                    )
                visit_kwargs = {
                    "command": command,
                    "entry": entry,
                    "candidate": candidate,
                    "source_capture_id": source_capture_id,
                }
                if reservation is not None:
                    visit_kwargs["menu_intent_id"] = reservation.intent_id
                result = await self._page_visitor.visit(**visit_kwargs)
                if not isinstance(result, WebMenuVisitResult):
                    raise TypeError("page_visitor 必须返回 WebMenuVisitResult")
            except Exception as exc:  # noqa: BLE001 - reservation 已消费，必须形成失败终态
                result = WebMenuVisitResult(
                    final_capture_id=None,
                    page_type=None,
                    status=WebMenuTerminalStatus.NAVIGATION_FAILED,
                    error_code="WEB_MENU_NAVIGATION_FAILED",
                    menu_action_count=int(reservation is not None),
                    menu_intent_id=(reservation.intent_id if reservation else None),
                    diagnostic_type=_diagnostic_type(exc),
                    diagnostic_message="菜单访问失败，已闭合当前 Catalog 账本",
                )
            unknown_no_href_outcome = (
                isinstance(entry, WebRuntimeMenuEntry)
                and entry.canonical_url is None
                and result.status
                not in {
                    WebMenuTerminalStatus.DETAIL_CAPTURED,
                    WebMenuTerminalStatus.LOGICAL_TARGET_REUSED,
                }
                and not is_proven_submenu_expansion(result)
            )
            if unknown_no_href_outcome:
                result = replace(
                    result,
                    error_code="WEB_DISCOVERY_UNKNOWN_OUTCOME_BLOCKED",
                )
                discovery_outcome_unproven = True
            if self._budget_ledger is not None:
                await self._budget_ledger.finalize(
                    _terminal_record(
                        command=command,
                        site_id=site.site_id,
                        entry=entry,
                        result=result,
                        reservation_menu_entry_id=(
                            reservation.menu_entry_id if reservation is not None else None
                        ),
                    )
                )
            ledger_by_entry_id[entry.entry_id] = _ledger(
                entry,
                status=result.status,
                source_capture_id=source_capture_id,
                final_capture_id=result.final_capture_id,
                page_type=result.page_type,
                detail_capture_id=result.detail_capture_id,
                error_code=result.error_code,
                menu_probe_capture_id=result.menu_probe_capture_id,
                menu_ready_capture_id=result.menu_ready_capture_id,
                menu_page_type=result.menu_page_type,
                menu_readiness_evidence_capture_ids=(result.menu_readiness_evidence_capture_ids),
                menu_dismissed_layers=result.menu_dismissed_layers,
                detail_candidate_ids=result.detail_candidate_ids,
                selected_detail_candidate_id=(result.selected_detail_candidate_id),
                detail_selection_confidence=(result.detail_selection_confidence),
                detail_selection_reason=result.detail_selection_reason,
                detail_selection_diagnostic=result.detail_selection_diagnostic,
                detail_probe_capture_id=result.detail_probe_capture_id,
                detail_ready_capture_id=result.detail_ready_capture_id,
                detail_page_type=result.detail_page_type,
                detail_readiness_evidence_capture_ids=(
                    result.detail_readiness_evidence_capture_ids
                ),
                detail_dismissed_layers=result.detail_dismissed_layers,
                diagnostic_type=result.diagnostic_type,
                diagnostic_message=result.diagnostic_message,
                menu_action_count=result.menu_action_count,
                menu_intent_id=result.menu_intent_id,
                menu_action_execution_id=result.menu_action_execution_id,
                detail_action_count=result.detail_action_count,
                detail_intent_id=result.detail_intent_id,
                detail_action_execution_id=result.detail_action_execution_id,
                ad_landing_evidence=result.ad_landing_evidence,
            )
            if result.final_capture_id:
                current_capture_id = result.final_capture_id
                page_timeline.append(result.final_capture_id)
            if result.navigation_handoff_error:
                # 当前菜单审计已经落库，不以旧基线继续，不发额外动作掩盖交接失败。
                await refresh_candidates()
                blocked_reconciliation = reconcile_site_menu(site, tuple(candidate_pool.values()))
                for observed in blocked_reconciliation.runtime_unreviewed:
                    discovery_reasons[discovery_entry_id(observed)] = (
                        discovery_safety_reason(observed, site_origin=site.origin)
                        or "WEB_DISCOVERY_UNKNOWN_OUTCOME_BLOCKED"
                    )
                await persist_discoveries(blocked_reconciliation)
                raise RuntimeError(result.navigation_handoff_error)
            if result.navigation_current_capture_id:
                current_capture_id = result.navigation_current_capture_id
                authoritative_current = True
            if result.page_type:
                page_type_keys.append(result.page_type)
            if result.status is WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED:
                site_human_verification_blocked = True
            pending_entries.remove(entry)
            frontier_remaining -= 1

            if site_human_verification_blocked:
                for pending in pending_entries:
                    ledger_by_entry_id[pending.entry_id] = _ledger(
                        pending,
                        status=WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED,
                        source_capture_id=current_capture_id,
                        error_code="WEB_SITE_HUMAN_VERIFICATION_BLOCKED",
                    )
                    if self._budget_ledger is not None:
                        await self._budget_ledger.finalize(
                            _zero_action_terminal_record(
                                command=command,
                                site_id=site.site_id,
                                entry=pending,
                                status=WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED,
                                capture_id=current_capture_id,
                                error_code="WEB_SITE_HUMAN_VERIFICATION_BLOCKED",
                            )
                        )
                break

        if not site_human_verification_blocked:
            for pending in pending_entries:
                ledger_by_entry_id[pending.entry_id] = _ledger(
                    pending,
                    status=WebMenuTerminalStatus.ALLOWLIST_DRIFTED,
                    source_capture_id=current_capture_id,
                    error_code="WEB_MENU_ALLOWLIST_DRIFTED",
                )
                if self._budget_ledger is not None:
                    await self._budget_ledger.finalize(
                        _zero_action_terminal_record(
                            command=command,
                            site_id=site.site_id,
                            entry=pending,
                            status=WebMenuTerminalStatus.ALLOWLIST_DRIFTED,
                            capture_id=current_capture_id,
                            error_code="WEB_MENU_ALLOWLIST_DRIFTED",
                        )
                    )
            frontier_remaining = 0

        await refresh_candidates()
        final_reconciliation = reconcile_site_menu(site, tuple(candidate_pool.values()))
        for observed in final_reconciliation.runtime_unreviewed:
            identity = discovery_entry_id(observed)
            if self.discovery_policy.enabled and identity not in discovery_reasons:
                discovery_reasons[identity] = (
                    "WEB_SITE_HUMAN_VERIFICATION_BLOCKED"
                    if site_human_verification_blocked
                    else discovery_safety_reason(observed, site_origin=site.origin)
                    or "WEB_DISCOVERY_BUDGET_EXHAUSTED"
                )
        await persist_discoveries(final_reconciliation)
        ledger = [
            ledger_by_entry_id[entry.entry_id]
            for entry in site.entries
            if entry.entry_id in ledger_by_entry_id
        ]
        runtime_discovered_ledger = tuple(
            WebRuntimeDiscoveredLedgerEntry(
                entry_id=candidate.entry_id,
                menu_path=candidate.menu_path,
                canonical_url=candidate.url,
                menu_position=candidate.menu_position,
                source_capture_id=(
                    candidate.discovery_capture_id
                    or candidate.source_capture_id
                    or initial_capture_id
                ),
                source_kind=candidate.source_kind,
                target_hint=candidate.target_hint,
                actionability=candidate.actionability,
                risk_level=candidate.risk_level,
                parent_menu_entry_id=candidate.parent_menu_entry_id,
                parent_logical_target_identity=candidate.parent_logical_target_identity,
                menu_depth=candidate.menu_depth,
                exploration_path=candidate.exploration_path,
                logical_target_identity=candidate.logical_target_identity,
            )
            for candidate in final_reconciliation.runtime_unreviewed
            if not _matches_non_executable_catalog_entry(candidate, site)
        )
        counts = Counter(item.status.value for item in ledger)
        status_counts = {
            status.value: counts.get(status.value, 0) for status in WebMenuTerminalStatus
        }
        if self._budget_ledger is not None:
            persisted = await self._read_persistent_summary(command, site)
            return replace(
                persisted,
                navigation_current_capture_id=(
                    current_capture_id if authoritative_current else None
                ),
            )
        return WebMenuExplorationSummary(
            site_id=site.site_id,
            catalog_version=site.catalog_version,
            catalog_sha256=context.content_sha256,
            menu_total=len(site.entries),
            menu_limit=menu_limit,
            executed_same_origin_count=executed_same_origin_count,
            menu_limit_skipped_count=len(menu_limit_skipped_entries),
            frontier_remaining=frontier_remaining,
            menu_ledger=tuple(ledger),
            runtime_discovered_ledger=runtime_discovered_ledger,
            status_counts=status_counts,
            page_timeline=tuple(page_timeline),
            page_type_keys=tuple(page_type_keys),
            navigation_current_capture_id=(
                current_capture_id if authoritative_current else None
            ),
        )

    async def _read_persistent_summary(
        self,
        command: RunWebWorkflowCommand,
        site: WebSiteMenuCatalog,
    ) -> WebMenuExplorationSummary:
        if self._budget_ledger is None:
            raise RuntimeError("Web 持久化预算账本未配置")
        context = self._navigation_context.resolve_site_context(command.target_url)
        summary = await self._budget_ledger.read_summary(
            agent_run_id=command.agent_run_id,
            site_id=site.site_id,
        )
        return hydrate_web_menu_summary(
            summary,
            site=site,
            catalog_version=site.catalog_version,
            catalog_sha256=context.content_sha256,
        )


def _runtime_discovery_execution_is_complete(execution: WebMenuLedgerEntry) -> bool:
    if execution.status in {
        WebMenuTerminalStatus.DETAIL_CAPTURED,
        WebMenuTerminalStatus.LOGICAL_TARGET_REUSED,
    }:
        return True
    return execution.submenu_relations_verified and persisted_submenu_expansion_has_valid_shape(
        execution.to_dict()
    )


def restore_persisted_navigation_state(
    candidate_source: object,
    summary: object,
    *,
    session_id: str,
) -> str | None:
    """把已跨表验证的展开父项及持久容器种子注入新的候选源。"""
    rows = tuple(getattr(summary, "runtime_discovered_ledger", ()) or ())
    completed = [
        row
        for row in rows
        if row.execution is not None
        and row.execution.diagnostic_type == "WEB_SUBMENU_EXPANDED"
        and _runtime_discovery_execution_is_complete(row.execution)
    ]
    if not completed:
        return None
    max_depth = max(int(row.menu_depth or 0) for row in completed)
    parents = [row for row in completed if int(row.menu_depth or 0) == max_depth]
    reject = getattr(candidate_source, "reject_navigation_restore", None)
    if len(parents) != 1:
        reason = "WEB_MENU_LINEAGE_AMBIGUOUS"
        if callable(reject):
            reject(session_id, reason=reason)
        return reason
    restored = tuple(
        candidate for row in rows if (candidate := row.restored_navigation_candidate()) is not None
    )
    parent = parents[0].restored_navigation_candidate()
    if parent is None or len(restored) != len(rows):
        reason = "WEB_MENU_LINEAGE_UNAVAILABLE"
        if callable(reject):
            reject(session_id, reason=reason)
        return reason
    restore = getattr(candidate_source, "restore_navigation_state", None)
    if not callable(restore):
        return "WEB_MENU_LINEAGE_UNAVAILABLE"
    return restore(
        session_id,
        candidates=restored,
        parent_candidate=parent,
        parent_menu_entry_id=parents[0].entry_id,
    )


def plan_web_menu_collection(
    site: WebSiteMenuCatalog,
    *,
    menu_limit: int | None = WEB_MENU_COLLECTION_LIMIT,
    discovery_policy: WebDiscoveryPolicy | None = None,
    required_page_types: tuple[str, ...] = (),
) -> WebMenuCollectionPlan:
    """过滤非业务入口并把同一规范 URL 的菜单路径合并为一次访问。"""

    if menu_limit is not None and (
        isinstance(menu_limit, bool)
        or not isinstance(menu_limit, int)
        or not 1 <= menu_limit <= 1000
    ):
        raise ValueError("menu_limit 必须为 1..1000 整数或 None")

    unique_entries: list[WebSiteMenuEntry] = []
    skipped_entries: list[WebSiteMenuEntry] = []
    primary_by_url = {
        canonical_web_logical_page_url(entry.canonical_url): entry
        for entry in site.entries
        if entry.decision is WebMenuDecision.HOME_BASELINE
    }
    alias_of: dict[str, str] = {}
    for entry in site.entries:
        if entry.decision is not WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY:
            continue
        if (
            entry.status is WebCatalogStatus.DEPRECATED
            or not entry.is_same_origin
            or not entry.collection_eligibility.eligible
        ):
            skipped_entries.append(entry)
            continue
        canonical_url = canonical_web_logical_page_url(entry.canonical_url)
        primary = primary_by_url.get(canonical_url)
        if primary is not None:
            skipped_entries.append(entry)
            alias_of[entry.entry_id] = primary.entry_id
            continue
        primary_by_url[canonical_url] = entry
        unique_entries.append(entry)

    capacity = len(unique_entries) if menu_limit is None else menu_limit
    if discovery_policy is not None and discovery_policy.enabled:
        # 这里只安排冻结类型的探索机会；任何菜单提示都不代表真实覆盖。
        opportunities = []
        remaining = list(unique_entries)
        for page_type in required_page_types:
            if not remaining:
                break
            same_family_list = page_type.replace("_detail", "_list")
            chosen = next(
                (
                    entry
                    for entry in remaining
                    if entry.classification_hint is not None
                    and entry.classification_hint.expected_page_type == page_type
                ),
                None,
            )
            chosen = chosen or next(
                (
                    entry
                    for entry in remaining
                    if entry.classification_hint is not None
                    and entry.classification_hint.expected_page_type == same_family_list
                ),
                None,
            )
            chosen = chosen or remaining[0]
            opportunities.append(chosen)
            remaining.remove(chosen)
        unique_entries = opportunities + remaining
        if menu_limit is not None:
            capacity = min(
                menu_limit,
                max(
                    len(opportunities),
                    menu_limit - (discovery_policy.max_unlisted_menus or menu_limit),
                ),
            )
    selected_entries = unique_entries[:capacity]
    skipped_entries.extend(unique_entries[capacity:])
    return WebMenuCollectionPlan(
        selected_entries=tuple(selected_entries),
        skipped_entries=tuple(skipped_entries),
        alias_of=alias_of,
    )


def hydrate_web_menu_summary(
    summary: WebMenuExplorationSummary,
    *,
    site: WebSiteMenuCatalog,
    catalog_version: str,
    catalog_sha256: str,
) -> WebMenuExplorationSummary:
    """按冻结 Registry entry_id 水合可读 URL，哈希占位绝不参与导航。"""

    expected_ids = tuple(entry.entry_id for entry in site.entries)
    persisted_ids = tuple(item.entry_id for item in summary.menu_ledger)
    if (
        summary.site_id != site.site_id
        or summary.catalog_version != catalog_version
        or summary.catalog_sha256 != catalog_sha256
        or summary.menu_total != len(site.entries)
        or len(summary.menu_ledger) != len(site.entries)
        or persisted_ids != expected_ids
        or len(set(persisted_ids)) != len(persisted_ids)
    ):
        raise WebExplorationBudgetLedgerConflictError("Web 持久化摘要与冻结 Catalog 身份不一致")
    hydrated: list[WebMenuLedgerEntry] = []
    for item, entry in zip(summary.menu_ledger, site.entries, strict=True):
        expected_placeholder = "sha256:" + sha256(entry.canonical_url.encode("utf-8")).hexdigest()
        if item.menu_path != entry.menu_path or item.canonical_url != expected_placeholder:
            raise WebExplorationBudgetLedgerConflictError(
                "Web 持久化摘要菜单路径或 URL 摘要与冻结 Catalog 不一致"
            )
        hydrated.append(
            replace(
                item,
                menu_path=entry.menu_path,
                canonical_url=entry.canonical_url,
            )
        )
    return replace(summary, menu_ledger=tuple(hydrated))


def _ledger(
    entry: WebSiteMenuEntry,
    *,
    status: WebMenuTerminalStatus,
    source_capture_id: str,
    final_capture_id: str | None = None,
    page_type: str | None = None,
    detail_capture_id: str | None = None,
    error_code: str | None = None,
    alias_of: str | None = None,
    menu_probe_capture_id: str | None = None,
    menu_ready_capture_id: str | None = None,
    menu_page_type: str | None = None,
    menu_readiness_evidence_capture_ids: tuple[str, ...] = (),
    menu_dismissed_layers: tuple[str, ...] = (),
    detail_candidate_ids: tuple[str, ...] = (),
    selected_detail_candidate_id: str | None = None,
    detail_selection_confidence: float | None = None,
    detail_selection_reason: str | None = None,
    detail_selection_diagnostic: Mapping[str, object] | None = None,
    detail_probe_capture_id: str | None = None,
    detail_ready_capture_id: str | None = None,
    detail_page_type: str | None = None,
    detail_readiness_evidence_capture_ids: tuple[str, ...] = (),
    detail_dismissed_layers: tuple[str, ...] = (),
    diagnostic_type: str | None = None,
    diagnostic_message: str | None = None,
    menu_action_count: int = 0,
    menu_intent_id: str | None = None,
    menu_action_execution_id: str | None = None,
    detail_action_count: int = 0,
    detail_intent_id: str | None = None,
    detail_action_execution_id: str | None = None,
    ad_landing_evidence: Mapping[str, object] | None = None,
) -> WebMenuLedgerEntry:
    return WebMenuLedgerEntry(
        entry_id=entry.entry_id,
        menu_path=entry.menu_path,
        canonical_url=entry.canonical_url,
        status=status,
        source_capture_id=source_capture_id,
        final_capture_id=final_capture_id,
        page_type=page_type,
        detail_capture_id=detail_capture_id,
        error_code=error_code,
        alias_of=alias_of,
        menu_probe_capture_id=menu_probe_capture_id,
        menu_ready_capture_id=menu_ready_capture_id,
        menu_page_type=menu_page_type,
        menu_readiness_evidence_capture_ids=menu_readiness_evidence_capture_ids,
        menu_dismissed_layers=menu_dismissed_layers,
        detail_candidate_ids=detail_candidate_ids,
        selected_detail_candidate_id=selected_detail_candidate_id,
        detail_selection_confidence=detail_selection_confidence,
        detail_selection_reason=detail_selection_reason,
        detail_selection_diagnostic=bounded_detail_selection_diagnostic(
            detail_selection_diagnostic
        ),
        detail_probe_capture_id=detail_probe_capture_id,
        detail_ready_capture_id=detail_ready_capture_id,
        detail_page_type=detail_page_type,
        detail_readiness_evidence_capture_ids=(detail_readiness_evidence_capture_ids),
        detail_dismissed_layers=detail_dismissed_layers,
        diagnostic_type=_bounded_diagnostic(diagnostic_type, limit=128),
        diagnostic_message=_bounded_diagnostic(diagnostic_message, limit=300),
        menu_action_count=menu_action_count,
        menu_intent_id=menu_intent_id,
        menu_action_execution_id=menu_action_execution_id,
        detail_action_count=detail_action_count,
        detail_intent_id=detail_intent_id,
        detail_action_execution_id=detail_action_execution_id,
        ad_landing_evidence=dict(ad_landing_evidence or {}),
    )


def _zero_action_terminal_record(
    *,
    command: RunWebWorkflowCommand,
    site_id: str,
    entry: WebSiteMenuEntry,
    status: WebMenuTerminalStatus,
    capture_id: str,
    error_code: str,
) -> WebMenuLedgerTerminalRecord:
    return WebMenuLedgerTerminalRecord(
        task_id=command.task_id,
        agent_run_id=command.agent_run_id,
        session_id=command.session_id,
        site_id=site_id,
        menu_entry_id=entry.entry_id,
        status=status,
        menu_probe_capture_id=capture_id,
        diagnostic={"error_code": error_code},
    )


def _terminal_record(
    *,
    command: RunWebWorkflowCommand,
    site_id: str,
    entry: WebSiteMenuEntry,
    result: WebMenuVisitResult,
    reservation_menu_entry_id: str | None = None,
) -> WebMenuLedgerTerminalRecord:
    has_detail_action = result.detail_action_count == 1
    selection = {}
    if result.selected_detail_candidate_id is not None:
        selection = {
            "candidate_id": result.selected_detail_candidate_id,
            "confidence": result.detail_selection_confidence,
            "reason": result.detail_selection_reason,
        }
    diagnostic = {
        key: value
        for key, value in {
            "error_code": result.error_code,
            "type": result.diagnostic_type,
            "message": result.diagnostic_message,
            "reused_capture_id": result.reused_capture_id,
            "reused_logical_page_id": result.reused_logical_page_id,
        }.items()
        if value is not None
    }
    selection_diagnostic = bounded_detail_selection_diagnostic(result.detail_selection_diagnostic)
    if selection_diagnostic:
        diagnostic["detail_selection_diagnostic"] = selection_diagnostic
    if result.reused_capture_id is not None:
        diagnostic["reused_target_stage"] = (
            ("detail_after_action" if has_detail_action else "detail_before_action")
            if result.detail_page_type is not None
            else "menu"
        )
    if result.primary_content_diagnostics:
        diagnostic["primary_content_diagnostics"] = list(result.primary_content_diagnostics)
    if result.navigation_handoff_error:
        diagnostic["navigation_handoff_error"] = result.navigation_handoff_error
    if result.ad_landing_evidence:
        diagnostic["ad_landing_evidence"] = dict(result.ad_landing_evidence)
    for name in ("menu_interaction_outcome", "detail_interaction_outcome"):
        outcome = getattr(result, name)
        if outcome is not None:
            diagnostic[name] = asdict(outcome)
    for name in (
        "detail_scroll_interaction_outcomes",
        "menu_dismissal_interaction_outcomes",
        "detail_dismissal_interaction_outcomes",
    ):
        outcomes = getattr(result, name)
        if outcomes:
            diagnostic[name] = [asdict(outcome) for outcome in outcomes]
    return WebMenuLedgerTerminalRecord(
        task_id=command.task_id,
        agent_run_id=command.agent_run_id,
        session_id=command.session_id,
        site_id=site_id,
        # 动态候选重发现时，运行时身份可能已被归并到既有账本行。
        # 终态必须引用 claim_menu 实际锁定并返回的 reservation 身份。
        menu_entry_id=reservation_menu_entry_id or entry.entry_id,
        status=result.status,
        menu_action_execution_id=result.menu_action_execution_id,
        menu_probe_capture_id=result.menu_probe_capture_id,
        menu_ready_capture_id=result.menu_ready_capture_id,
        menu_page_type=result.menu_page_type,
        detail_action_execution_id=(
            result.detail_action_execution_id if has_detail_action else None
        ),
        detail_probe_capture_id=(result.detail_probe_capture_id if has_detail_action else None),
        detail_ready_capture_id=(result.detail_ready_capture_id if has_detail_action else None),
        detail_page_type=(result.detail_page_type if has_detail_action else None),
        detail_selection=selection,
        diagnostic=diagnostic,
    )


def _diagnostic_type(exc: Exception) -> str:
    raw = type(exc).__name__
    normalized = "".join(
        character if character.isalnum() or character in {"_", "."} else "_" for character in raw
    )
    return normalized[:128] or "Exception"


def _bounded_diagnostic(value: str | None, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).strip()
    return normalized[:limit] or None


def _require_detail_action_count(value: object) -> None:
    _require_action_count(value, "detail_action_count")


def _require_action_count(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1:
        raise ValueError(f"{field_name} 必须为 0 或 1")


def _matches_non_executable_catalog_entry(
    candidate: WebNavigationCandidate,
    site: WebSiteMenuCatalog,
) -> bool:
    if candidate.url is None:
        return False
    return any(
        (
            entry.decision in {WebMenuDecision.HOME_BASELINE, WebMenuDecision.BLOCKED_CROSS_ORIGIN}
            or entry.status is WebCatalogStatus.DEPRECATED
            or not entry.collection_eligibility.eligible
        )
        and canonical_navigation_identity(entry.canonical_url)
        == canonical_navigation_identity(candidate.url)
        and normalized_menu_path(entry.menu_path) == normalized_menu_path(candidate.menu_path)
        for entry in site.entries
    )


def _candidate_is_executable(candidate: WebNavigationCandidate) -> bool:
    return (
        candidate.url is not None
        and candidate.actionability == "actionable"
        and candidate.risk_level == "low"
    )


__all__ = [
    "WebMenuExplorer",
    "WebMenuExplorationSummary",
    "WebMenuLedgerEntry",
    "WebMenuPageVisitor",
    "WebMenuReconciliation",
    "WebMenuVisitResult",
    "WebNavigationCandidateSource",
    "WebRuntimeDiscoveredLedgerEntry",
    "hydrate_web_menu_summary",
    "reconcile_site_menu",
]
