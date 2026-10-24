"""App 流程基建运行时桥：内存守卫 + 可选 PostgreSQL 持久化。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from services.api.app.application.device_pool import DeviceLease
from services.api.app.application.flow_foundation_control import (
    FlowFoundationControlError,
    FlowFoundationControlService,
)
from services.api.app.application.flow_foundation_observation_sequence import (
    late_fill_observation_sequence,
    seal_observation_sequence,
)
from services.api.app.application.flow_foundation_p1 import (
    FlowFoundationP1Error,
    FlowFoundationP1Service,
)
from services.api.app.application.flow_foundation_parent_hold import ParentHoldGate
from services.api.app.domain.exploration.flow_foundation import (
    ArtifactCommitment,
    CaptureReservation,
    DeviceLeaseHandoff,
    TargetPlatform,
)
from services.api.app.domain.exploration.flow_foundation_control import (
    TriggerDecisionKind,
    TriggerKeyParts,
)
from services.api.app.domain.exploration.flow_foundation_definitions import (
    APP_DETAIL_PURE_OBSERVATION,
    APP_LOW_RISK_EXPAND_CLOSE_RETURN,
    FlowDefinition,
    get_flow_definition,
)
from services.api.app.domain.exploration.flow_foundation_evidence import (
    EvidenceSequenceSeal,
    ObservationClockDomain,
    ObservationSequenceDraft,
    ResourceEventSeqAllocation,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.flow_foundation_repository import (
    PostgresFlowFoundationRepository,
)

logger = logging.getLogger(__name__)


@dataclass
class AppFlowFoundationRuntime:
    """组合根持有的流程基建运行时：P1＋控制服务，可选异步仓储。"""

    p1: FlowFoundationP1Service
    control: FlowFoundationControlService
    repo: PostgresFlowFoundationRepository | None = None
    enabled: bool = True
    _memory_observations: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _memory_claims: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _memory_seals: dict[str, EvidenceSequenceSeal] = field(
        default_factory=dict, repr=False
    )

    async def record_capture_reservation_and_commitment(
        self,
        *,
        task_id: str,
        phase_run_id: str,
        checkpoint_key: str,
        sample_index: int,
        producer_epoch: str,
        capture_id: str,
        content_digest: str,
        content_length: int,
        fencing_token: str | int,
        session_id: str | None = None,
        action_execution_id: str | None = None,
        execution_segment_id: str | None = None,
        artifact_slot: str = "screenshot",
        pure_observation: bool = False,
    ) -> tuple[CaptureReservation, ArtifactCommitment]:
        if not self.enabled:
            raise FlowFoundationP1Error(
                "流程基建运行时未启用",
                error_code="FLOW_FOUNDATION_RUNTIME_DISABLED",
            )
        reservation = self.p1.reserve_capture(
            task_id=task_id,
            phase_run_id=phase_run_id,
            checkpoint_key=checkpoint_key,
            sample_index=sample_index,
            producer_epoch=producer_epoch,
            capture_id=capture_id,
            session_id=session_id,
            action_execution_id=action_execution_id,
            execution_segment_id=execution_segment_id,
            artifact_slots=(artifact_slot,),
            pure_observation=pure_observation,
        )
        commitment = self.p1.commit_artifact(
            reservation=reservation,
            artifact_slot=artifact_slot,
            content_digest=content_digest,
            content_length=content_length,
            fencing_token=fencing_token,
        )
        if self.repo is not None:
            await self.repo.upsert_reservation(reservation)
            await self.repo.upsert_commitment(commitment)
        return reservation, commitment

    async def allocate_observation_event(
        self,
        *,
        task_id: str,
        phase_run_id: str,
        resource_id: str,
        producer_epoch: str,
        sample_index: int,
        capture_id: str,
        content_digest: str,
        clock_domain: ObservationClockDomain = ObservationClockDomain.PRODUCER_MONOTONIC,
    ) -> ResourceEventSeqAllocation:
        if not self.enabled:
            raise FlowFoundationControlError(
                "流程基建运行时未启用",
                error_code="FLOW_FOUNDATION_RUNTIME_DISABLED",
            )
        if self.repo is not None:
            seq = await self.repo.next_resource_event_seq(
                resource_id=resource_id,
                producer_epoch=producer_epoch,
            )
            allocation = ResourceEventSeqAllocation(
                resource_id=resource_id,
                producer_epoch=producer_epoch,
                resource_event_seq=seq,
                sample_index=sample_index,
                clock_domain=clock_domain,
            )
        else:
            allocation = self.control.allocate_resource_event_seq(
                resource_id=resource_id,
                producer_epoch=producer_epoch,
                sample_index=sample_index,
                clock_domain=clock_domain,
            )
        event = {
            "task_id": task_id,
            "phase_run_id": phase_run_id,
            "resource_id": allocation.resource_id,
            "producer_epoch": allocation.producer_epoch,
            "resource_event_seq": allocation.resource_event_seq,
            "sample_index": allocation.sample_index,
            "capture_id": capture_id,
            "clock_domain": allocation.clock_domain.value,
            "content_digest": content_digest,
        }
        self._memory_observations.append(event)
        if self.repo is not None:
            await self.repo.insert_observation_event(**event)
        return allocation

    def evaluate_and_maybe_claim_after_observation(
        self,
        *,
        task_id: str,
        exploration_scope_id: str,
        phase_run_id: str,
        parent_work_kind: str,
        parent_work_id: str,
        parent_version: int,
        owner_execution_segment_id: str,
        page_type: str | None,
        subject_ref: str | None = None,
        observation_fresh: bool = True,
        subject_confirmed: bool = True,
        has_requirement_or_explore_goal: bool = True,
        capabilities_ready: bool = True,
        budget_units: int = 1,
        flow_code: str | None = None,
    ) -> dict[str, Any]:
        """观察后、主探索动作前的触发评估；eligible 时 claim（内存，可选异步持久化由调用方）。"""

        if not self.enabled:
            return {
                "kind": "skipped",
                "reason_code": "runtime_disabled",
                "flow_code": None,
                "claimed": False,
            }
        definition = _select_definition_for_page(
            page_type=page_type,
            flow_code=flow_code,
        )
        if definition is None:
            # 首页／开屏等主探索页不挂短流程（QC e7242d9e：home 误 claim
            # → 跨 Work 后继触发「短流程禁止递归」整轮失败）。
            return {
                "kind": "skipped",
                "reason_code": "no_short_flow_for_page",
                "flow_code": None,
                "claimed": False,
                "phase_run_id": phase_run_id,
                "page_type": str(page_type or "").strip() or None,
            }
        trigger_state = {
            "page_type": str(page_type or "").strip().casefold() or "unknown",
            "subject_ref": str(subject_ref or "").strip() or "none",
        }
        parts = TriggerKeyParts(
            task_id=task_id,
            exploration_scope_id=exploration_scope_id,
            target_platform=TargetPlatform.APP,
            flow_code=definition.code,
            definition_hash=definition.definition_hash,
            target_scope="app",
            semantic_page_identity=str(page_type or "unknown"),
            subject_or_occurrence=str(subject_ref or "none"),
            trigger_state_hash=stable_protocol_hash(trigger_state),
            requirement_contract_hash=definition.definition_hash,
        )
        decision = self.control.evaluate_trigger_candidate(
            parts=parts,
            trigger_state_payload=trigger_state,
            observation_fresh=observation_fresh,
            subject_confirmed=subject_confirmed,
            has_requirement_or_explore_goal=has_requirement_or_explore_goal,
            capabilities_ready=capabilities_ready,
            capabilities_partial_accepted=True,
            permission_allowed=True,
            budget_sufficient=budget_units > 0,
        )
        result: dict[str, Any] = {
            "kind": decision.kind.value,
            "reason_code": decision.reason_code.value,
            "trigger_key": decision.trigger_key,
            "flow_code": definition.code,
            "definition_hash": definition.definition_hash,
            "missing_capabilities": list(decision.missing_capabilities),
            "claimed": False,
            "phase_run_id": phase_run_id,
        }
        if decision.kind is TriggerDecisionKind.ELIGIBLE:
            try:
                claim = self.control.claim_flow(
                    decision=decision,
                    phase_run_id=phase_run_id,
                    parent_work_kind=parent_work_kind,
                    parent_work_id=parent_work_id,
                    parent_version=parent_version,
                    owner_execution_segment_id=owner_execution_segment_id,
                    budget_units=budget_units,
                    task_id=task_id,
                    expected_parent_version=parent_version,
                )
            except FlowFoundationControlError as exc:
                # claim 失败不得炸死主探索轮次；记 skipped 继续点「立即看」／底栏。
                logger.warning(
                    "flow_foundation_claim_skipped task_id=%s parent=%s err=%s",
                    task_id,
                    parent_work_id,
                    exc,
                )
                result["kind"] = "skipped"
                result["reason_code"] = str(getattr(exc, "error_code", None) or "claim_rejected")
                result["claim_error"] = str(exc)
                return result
            result["claimed"] = True
            result["consumption_id"] = claim.consumption_id
            result["hold_id"] = claim.hold_id
            result["parent_work_id"] = claim.parent_work_id
            result["parent_version"] = claim.parent_version
            self._memory_claims.append(
                {
                    "task_id": task_id,
                    "phase_run_id": phase_run_id,
                    "trigger_key": claim.trigger_key,
                    "flow_code": definition.code,
                    "definition_hash": definition.definition_hash,
                    "control_status": "claimed",
                    "execution_result": "pending",
                    "evidence_state": "pending",
                    "resume_result": "not_required",
                    "budget_consumption_id": claim.consumption_id,
                    "parent_hold_id": claim.hold_id,
                    "payload": {
                        "parent_work_id": claim.parent_work_id,
                        "parent_version": claim.parent_version,
                    },
                }
            )
        return result

    async def assert_parent_claimable(self, parent_work_id: str) -> None:
        """探索选取下一动作／触发 claim 前：父待办不得已被活动流程 hold。

        runtime 未启用时为 no-op，避免破坏既有单测。
        """

        if not self.enabled:
            return
        await ParentHoldGate(self).ensure_claimable(parent_work_id)

    def seal_series(
        self,
        draft: ObservationSequenceDraft,
        *,
        input_set_id: str | None = None,
        task_terminal: bool = False,
    ) -> EvidenceSequenceSeal:
        """内存封存观察序列；不引入新迁移。"""

        sealed = seal_observation_sequence(
            draft,
            input_set_id=input_set_id,
            task_terminal=task_terminal,
        )
        self._memory_seals[draft.series_id] = sealed
        return sealed

    def late_fill_series(
        self,
        *,
        series_id: str,
        late_fill: bool = True,
        mutate_old_input_set: bool = False,
        reopen_terminal_task: bool = False,
    ) -> EvidenceSequenceSeal:
        """对已封存序列做迟到补齐（新 revision，内存登记）。"""

        sealed = self._memory_seals.get(series_id)
        if sealed is None:
            raise FlowFoundationControlError(
                f"观察序列未封存：{series_id}",
                error_code="OBSERVATION_SEQUENCE_NOT_SEALED",
            )
        newer = late_fill_observation_sequence(
            sealed=sealed,
            late_fill=late_fill,
            mutate_old_input_set=mutate_old_input_set,
            reopen_terminal_task=reopen_terminal_task,
        )
        self._memory_seals[series_id] = newer
        return newer

    async def persist_claim_decision(
        self,
        *,
        task_id: str,
        decision: Mapping[str, Any],
    ) -> None:
        """将内存 claim 决策写穿到 PostgreSQL（可选）。"""

        if self.repo is None or not decision.get("claimed"):
            return
        from services.api.app.domain.exploration.flow_foundation_control import (
            FlowClaimRecord,
        )

        claim = FlowClaimRecord(
            trigger_key=str(decision["trigger_key"]),
            phase_run_id=str(decision["phase_run_id"]),
            consumption_id=str(decision["consumption_id"]),
            hold_id=str(decision["hold_id"]),
            parent_work_id=str(
                decision.get("parent_work_id")
                or f"work:{decision['phase_run_id']}"
            ),
            parent_version=int(decision.get("parent_version") or 0),
        )
        await self.repo.upsert_claim(
            claim=claim,
            task_id=task_id,
            flow_code=str(decision["flow_code"]),
            definition_hash=str(decision["definition_hash"]),
        )

    def handoff_device_lease_session(
        self,
        *,
        predecessor: DeviceLease,
        successor_session_id: str,
        predecessor_channel_silenced: bool,
    ) -> DeviceLeaseHandoff:
        """领域校验租约承接；DB 轮转由仓储 handoff_to_successor_session 完成。"""

        return self.p1.build_lease_handoff(
            predecessor=predecessor,
            successor_session_id=successor_session_id,
            successor_fencing_token=int(predecessor.fencing_token) + 1,
            predecessor_channel_silenced=predecessor_channel_silenced,
        )

    async def list_flows_for_task(self, *, task_id: str) -> list[dict[str, Any]]:
        if self.repo is not None:
            return await self.repo.list_flows_for_task(task_id=task_id)
        return [item for item in self._memory_claims if item.get("task_id") == task_id]

    async def list_flow(
        self, *, task_id: str, phase_run_id: str
    ) -> dict[str, Any] | None:
        if self.repo is not None:
            row = await self.repo.get_claim(phase_run_id=phase_run_id)
            if row and str(row.get("task_id")) == str(task_id):
                return row
            return None
        for item in self._memory_claims:
            if (
                item.get("task_id") == task_id
                and item.get("phase_run_id") == phase_run_id
            ):
                return item
        return None

    async def list_observations_for_phase(
        self, *, task_id: str, phase_run_id: str
    ) -> list[dict[str, Any]]:
        if self.repo is not None:
            return await self.repo.list_observations_for_phase(
                task_id=task_id,
                phase_run_id=phase_run_id,
            )
        return [
            item
            for item in self._memory_observations
            if item.get("task_id") == task_id
            and item.get("phase_run_id") == phase_run_id
        ]

    async def list_flow_links_for_capture(
        self, *, task_id: str, capture_id: str
    ) -> list[dict[str, Any]]:
        if self.repo is not None:
            return await self.repo.list_flow_links_for_capture(
                task_id=task_id,
                capture_id=capture_id,
            )
        return [
            {
                "task_id": item.get("task_id"),
                "phase_run_id": item.get("phase_run_id"),
                "capture_id": item.get("capture_id"),
                "resource_id": item.get("resource_id"),
                "resource_event_seq": item.get("resource_event_seq"),
                "sample_index": item.get("sample_index"),
                "content_digest": item.get("content_digest"),
            }
            for item in self._memory_observations
            if item.get("task_id") == task_id and item.get("capture_id") == capture_id
        ]


def build_app_flow_foundation_runtime(
    *,
    repo: PostgresFlowFoundationRepository | None = None,
    enabled: bool = True,
) -> AppFlowFoundationRuntime:
    return AppFlowFoundationRuntime(
        p1=FlowFoundationP1Service(),
        control=FlowFoundationControlService(),
        repo=repo,
        enabled=enabled,
    )


def _select_definition_for_page(
    *,
    page_type: str | None,
    flow_code: str | None,
) -> FlowDefinition | None:
    """按页型挑选短流程；主探索页返回 None（不 claim）。"""

    if flow_code:
        return get_flow_definition(flow_code)
    normalized = str(page_type or "").strip().casefold()
    if not normalized or normalized in {
        "home",
        "startup_ad",
        "splash",
        "countdown",
        "unknown",
        "profile",
        "mine",
        "library",
    }:
        return None
    if any(
        token in normalized
        for token in ("startup", "splash", "launch", "countdown", "_list", "list")
    ) and "detail" not in normalized:
        return None
    if any(
        token in normalized for token in ("detail", "player", "playback", "content", "video")
    ):
        return APP_DETAIL_PURE_OBSERVATION
    if any(token in normalized for token in ("expand", "sheet", "dialog", "popup")):
        return APP_LOW_RISK_EXPAND_CLOSE_RETURN
    return None
