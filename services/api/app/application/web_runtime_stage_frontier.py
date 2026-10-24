"""阶段驱动的菜单候选投影，复用现有唯一槽位和导航器。"""

from __future__ import annotations

import inspect
from dataclasses import replace

from services.api.app.application.web_capture_stage_failure import stage_failure_from_code
from services.api.app.application.web_exploration_budget_ledger import (
    WebMenuClaimRejection,
    WebMenuLedgerTerminalRecord,
)
from services.api.app.application.web_menu_exploration import WebMenuVisitResult, _terminal_record
from services.api.app.application.web_page_classification import (
    WEB_DETAIL_PAGE_TYPES,
    WEB_LIST_PAGE_TYPES,
)
from services.api.app.application.web_run_stage_orchestrator import WebStageFailure
from services.api.app.application.web_runtime_discovery import (
    WebRuntimeMenuEntry,
    discovery_safety_reason,
)
from services.api.app.application.web_stage_classification_reuse import read_reused_classification
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.page import WebMenuDecision


class WebRuntimeStageFrontier:
    def __init__(self, *, ledger, candidate_source, navigator, site, evidence=None):
        self._ledger = ledger
        self._candidate_source = candidate_source
        self._navigator = navigator
        self._site = site
        self._evidence = evidence

    async def has_budget(self, command):
        reader = getattr(self._ledger, "has_budget", None)
        return await reader(command=command, site_id=self._site.site_id) if reader else True

    async def reconcile(self, command, cursor):
        if not await self.has_budget(command):
            return
        if cursor.logical_page_key.startswith("detail:"):
            # 详情动作已归属原列表唯一槽位；独立逻辑页仅承接发现和报告，不重复记账。
            return
        root = cursor.logical_page_key == "home"
        if not root:
            summary = await self._ledger.read_summary(
                agent_run_id=command.agent_run_id, site_id=self._site.site_id
            )
            for row in summary.runtime_discovered_ledger:
                if row.entry_id != cursor.cursor_payload["menu_ledger_id"]:
                    continue
                persisted = getattr(row, "persisted_status", None)
                if row.execution is not None and persisted not in {
                    "menu_reserved",
                    "detail_reserved",
                }:
                    return
                if (
                    persisted == "runtime_discovered_unreviewed"
                    and row.execution is None
                    and row.execution_slot is None
                    and "WEB_EXPLORATION_DEADLINE_EXHAUSTED" in cursor.gap_codes
                ):
                    # 零动作候选保留发现事实；期限缺口由该 entry 的 checkpoint 审计。
                    return
        entry_id = (
            next(
                item.entry_id
                for item in self._site.entries
                if item.decision is WebMenuDecision.HOME_BASELINE
            )
            if root
            else cursor.cursor_payload["menu_ledger_id"]
        )
        failures = [
            gap
            for gap in cursor.gaps
            if gap.stage.value
            in {
                "bootstrap_navigation",
                "readiness",
                "primary_capture",
                "candidate_navigation",
            }
        ]
        capture_id = cursor.cursor_payload.get("capture_id")
        if failures or not capture_id:
            record = WebMenuLedgerTerminalRecord(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                site_id=self._site.site_id,
                menu_entry_id=entry_id,
                status=WebMenuTerminalStatus.NAVIGATION_FAILED,
                diagnostic={
                    "error_code": failures[-1].code if failures else "WEB_EVIDENCE_INCOMPLETE"
                },
            )
        else:
            reused = await read_reused_classification(self._evidence, cursor) if not root else None
            facts = await self._evidence.capture_outcome(capture_id=capture_id)
            probe_id = cursor.cursor_payload.get(
                "menu_navigation_capture_id",
                cursor.cursor_payload.get("parent_capture_id", capture_id),
            )
            probe = await self._evidence.capture_outcome(capture_id=probe_id)
            page_type = reused[1]["page_type"] if reused is not None else facts["page_type"]
            recognized = (
                page_type == "home"
                if root
                else page_type in (
                    WEB_DETAIL_PAGE_TYPES | WEB_LIST_PAGE_TYPES | {"collection_page"}
                )
            )
            status = (
                WebMenuTerminalStatus.LOGICAL_TARGET_REUSED
                if reused is not None
                else WebMenuTerminalStatus.HOME_BASELINE
                if root and recognized
                else WebMenuTerminalStatus.DETAIL_CAPTURED
                if not root and page_type in WEB_DETAIL_PAGE_TYPES
                else WebMenuTerminalStatus.MENU_PAGE_CAPTURED
                if not root and recognized
                else WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED
            )
            record = WebMenuLedgerTerminalRecord(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                site_id=self._site.site_id,
                menu_entry_id=entry_id,
                status=status,
                menu_action_execution_id=None if root else probe.get("action_execution_id"),
                menu_probe_capture_id=probe_id,
                menu_ready_capture_id=capture_id,
                menu_page_type=page_type if recognized else None,
                diagnostic={
                    **({"stage_gap_codes": list(cursor.gap_codes)} if cursor.gaps else {}),
                    **({} if recognized or reused is not None else {
                        "error_code": "WEB_PAGE_TYPE_UNCLASSIFIED",
                    }),
                    **({
                        "reused_target_stage": "menu",
                        "reused_capture_id": reused[0].reused_capture_id,
                        "reused_logical_page_id": reused[0].reused_logical_page_id,
                    } if reused is not None else {}),
                },
            )
        await self._ledger.finalize(record)
        if root:
            await self._ledger.seal(command=command, site_id=self._site.site_id)

    async def finalize_detail(self, command, cursor, *, detail, page_type):
        candidate = await self.get_candidate(
            command, entry_id=cursor.cursor_payload["menu_ledger_id"]
        )
        probe_id = cursor.cursor_payload.get(
            "menu_navigation_capture_id",
            cursor.cursor_payload.get("parent_capture_id", cursor.cursor_payload["capture_id"]),
        )
        probe = await self._evidence.capture_outcome(capture_id=probe_id)
        classified_ready_id = cursor.cursor_payload["capture_id"]
        menu_action_execution_id = probe.get("action_execution_id")
        menu_ready_capture_id = classified_ready_id
        if detail.status is WebMenuTerminalStatus.LOGICAL_TARGET_REUSED:
            menu_ready_capture_id = await self._evidence.current_menu_capture_after_primary(
                site_id=self._site.site_id,
                menu_entry_id=cursor.cursor_payload["menu_ledger_id"],
                menu_action_execution_id=menu_action_execution_id,
                menu_probe_capture_id=probe_id,
                menu_ready_capture_id=classified_ready_id,
            )
        result = WebMenuVisitResult(
            final_capture_id=menu_ready_capture_id,
            page_type=page_type,
            status=detail.status,
            error_code=detail.error_code,
            menu_probe_capture_id=probe_id,
            menu_ready_capture_id=menu_ready_capture_id,
            menu_page_type=page_type,
            menu_action_count=1,
            menu_action_execution_id=menu_action_execution_id,
            detail_capture_id=detail.detail_capture_id,
            detail_probe_capture_id=detail.probe_capture_id,
            detail_ready_capture_id=detail.detail_capture_id,
            detail_page_type=detail.page_type,
            detail_action_count=detail.detail_action_count,
            detail_intent_id=detail.detail_intent_id,
            detail_action_execution_id=detail.detail_action_execution_id,
            selected_detail_candidate_id=detail.selected_candidate_id,
            detail_candidate_ids=detail.detail_candidate_ids,
            detail_selection_confidence=detail.detail_selection_confidence,
            detail_selection_reason=detail.detail_selection_reason,
            detail_selection_diagnostic=detail.detail_selection_diagnostic,
            detail_readiness_evidence_capture_ids=detail.readiness_evidence_capture_ids,
            detail_dismissed_layers=detail.dismissed_layers,
            detail_interaction_outcome=detail.interaction_outcome,
            detail_scroll_interaction_outcomes=detail.scroll_interaction_outcomes,
            detail_dismissal_interaction_outcomes=detail.readiness_interaction_outcomes,
            diagnostic_type=detail.diagnostic_type,
            diagnostic_message=detail.diagnostic_message,
            reused_capture_id=detail.reused_capture_id,
            reused_logical_page_id=detail.reused_logical_page_id,
        )
        record = _terminal_record(
            command=command,
            site_id=self._site.site_id,
            entry=WebRuntimeMenuEntry.from_candidate(candidate),
            result=result,
            reservation_menu_entry_id=cursor.cursor_payload["menu_ledger_id"],
        )
        if menu_ready_capture_id != classified_ready_id:
            record = replace(
                record,
                diagnostic={
                    **record.diagnostic,
                    "menu_classification_capture_id": classified_ready_id,
                },
            )
        await self._ledger.finalize(record)

    async def discover(self, command, *, source_capture_id, page_role=None):
        self._candidate_source.bind_latest_navigation_capture(command.session_id, source_capture_id)
        bind_role = getattr(self._candidate_source, "bind_navigation_page_role", None)
        if callable(bind_role):
            if page_role is None:
                facts = await self._evidence.capture_outcome(capture_id=source_capture_id)
                page_type = facts.get("page_type")
                page_role = (
                    "home"
                    if page_type == "home"
                    else "detail"
                    if page_type in WEB_DETAIL_PAGE_TYPES
                    else "menu"
                )
            bind_role(command.session_id, source_capture_id, page_role)
        candidates = self._candidate_source.navigation_candidates(command.session_id)
        if inspect.isawaitable(candidates):
            candidates = await candidates
        for discovery_rank, candidate in enumerate(candidates):
            await self._ledger.record_discovery(
                command=command,
                site=self._site,
                candidate=candidate,
                source_capture_id=source_capture_id,
                error_code=discovery_safety_reason(candidate, site_origin=self._site.origin)
                or "WEB_DISCOVERY_PENDING",
                discovery_rank=discovery_rank,
            )

    async def detail_authority(self, command, cursor):
        """旧操作 marker 从已结算的列表详情账本恢复，不推断新正文帧的类型。"""
        summary = await self._ledger.read_summary(
            agent_run_id=command.agent_run_id, site_id=self._site.site_id
        )
        for row in summary.runtime_discovered_ledger:
            if row.entry_id == cursor.cursor_payload["menu_ledger_id"] and row.execution:
                record = row.execution
                if record.detail_page_type not in WEB_DETAIL_PAGE_TYPES:
                    return None
                capture_id = record.detail_ready_capture_id
                if capture_id:
                    facts = await self._evidence.capture_outcome(capture_id=capture_id)
                    if facts["page_type"] == record.detail_page_type:
                        return capture_id, record.detail_page_type
        raise WebStageFailure("WEB_DETAIL_AUTHORITY_MISSING")

    async def get_candidate(self, command, *, entry_id):
        summary = await self._ledger.read_summary(
            agent_run_id=command.agent_run_id, site_id=self._site.site_id
        )
        for row in summary.runtime_discovered_ledger:
            if row.entry_id == entry_id:
                candidate = row.restored_navigation_candidate()
                if candidate is not None and not discovery_safety_reason(
                    candidate, site_origin=self._site.origin
                ):
                    return candidate
        raise WebStageFailure("WEB_DISCOVERY_MANIFEST_INVALID")

    async def next_candidate(self, command, *, excluded_entry_ids):
        summary = await self._ledger.read_summary(
            agent_run_id=command.agent_run_id, site_id=self._site.site_id
        )
        menu_limit = getattr(summary, "menu_limit", None)
        if (
            menu_limit is not None
            and getattr(summary, "executed_same_origin_count", 0) >= menu_limit
        ):
            # 预算外 href 由持久化发现账本审计，不能再签发导航 checkpoint。
            closeout = getattr(self._ledger, "closeout_unexecuted", None)
            if callable(closeout):
                await closeout(command=command, site_id=self._site.site_id)
            return None
        for row in summary.runtime_discovered_ledger:
            if row.entry_id in excluded_entry_ids or row.execution is not None:
                continue
            candidate = row.restored_navigation_candidate()
            if candidate is None or discovery_safety_reason(
                candidate, site_origin=self._site.origin
            ):
                continue
            return row.entry_id, candidate
        return None

    async def navigate(self, command, *, candidate, source_capture_id):
        entry = WebRuntimeMenuEntry.from_candidate(candidate)
        read_current = getattr(self._evidence, "current_capture_reference", None)
        if read_current is not None:
            # 冻结发现证据仍保留原 Capture；预算领取和 GOTO 必须使用此刻的动作源。
            source_capture_id = (await read_current())["capture_id"]
        reservation = await self._ledger.claim_menu(
            command=command,
            entry=entry,
            source_capture_id=source_capture_id,
        )
        if reservation is None or isinstance(reservation, WebMenuClaimRejection):
            raise WebStageFailure("WEB_DISCOVERY_BUDGET_EXHAUSTED")
        result = await self._navigator.visit(
            command=command,
            entry=entry,
            candidate=candidate,
            source_capture_id=source_capture_id,
            menu_intent_id=reservation.intent_id,
        )
        if result.status is WebMenuTerminalStatus.NAVIGATION_FAILED:
            raise stage_failure_from_code(
                result.error_code or "WEB_MENU_NAVIGATION_FAILED",
                stage="menu_navigation", transient=True,
            )
        if result.status is WebMenuTerminalStatus.UNSAFE_NAVIGATION:
            raise stage_failure_from_code(
                result.error_code or "WEB_NAVIGATION_CURRENT_UNTRUSTED", stage="menu_navigation",
            )
        if not result.final_capture_id:
            raise WebStageFailure("WEB_PRIMARY_CAPTURE_FAILED", transient=True)
        return result
