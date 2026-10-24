"""H5 补采只从恢复后现场候选中选择一条满足证据目标的动作。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal, Protocol
from urllib.parse import urlsplit

from services.api.app.agents.explorer.maf_agent import ExplorerPrematureTerminalError
from services.api.app.application.capture_coordination import CaptureCoordinationError
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.evidence_recovery_replay import EvidenceRecoveryReplayError
from services.api.app.application.explorer_turn import ExplorerDecisionPort
from services.api.app.application.h5_probe_targets import (
    PROBE_TARGET_LABELS,
    PROBE_TARGET_MODE,
    probe_subject_key,
    select_cover_subject,
    select_probe_target,
)
from services.api.app.application.interaction_runtime import TrustedUiSnapshotReader
from services.api.app.domain.exploration import (
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    FrontierSnapshot,
    ProbeWork,
    SideEffectPolicy,
    build_exploration_work,
)
from services.api.app.domain.exploration.h5_frontier import H5_TASK_FRONTIER_METADATA_KEY
from services.api.app.domain.inspection import (
    EvidenceRequest,
    EvidenceSideEffectPolicy,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.protocol import stable_protocol_hash


class H5ProbeFrontierReader(Protocol):
    async def read_probe_frontier(self, *, work: ProbeWork) -> FrontierSnapshot: ...


class H5ProbeGoalPlanner:
    """复用 Explorer 的候选选择门禁，不新增队列或扩大补采预算。"""

    def __init__(
        self,
        *,
        delegate: ExplorerDecisionPort,
        frontiers: H5ProbeFrontierReader,
        now: Callable[[], datetime] | None = None,
        h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
        snapshot_reader: TrustedUiSnapshotReader | None = None,
    ):
        if h5_visual_coordinate_mode not in {"off", "shadow", "enforced"}:
            raise ValueError("h5_visual_coordinate_mode 必须为 off、shadow 或 enforced")
        self._delegate = delegate
        self._frontiers = frontiers
        self._snapshot_reader = snapshot_reader
        self._now = now or (lambda: datetime.now(UTC))
        self._h5_visual_coordinate_mode = h5_visual_coordinate_mode

    async def plan(
        self,
        *,
        work: ProbeWork,
        baseline: ActionIntent,
    ) -> ActionIntent | ExplorationTerminal:
        request = work.evidence_request
        goal_capability = (
            request.required_interaction.parameters.get("goal_capability")
            if request.required_interaction is not None
            else None
        )
        if (
            request.required_interaction is not None
            and goal_capability
            not in {
                "page_tab_navigation",
                "vertical_scroll",
                "cover_content_pair",
                "media_fullscreen_pair",
                "payment_entry_pair",
                "payment_plan_selection",
                "payment_plan_reveal",
                *PROBE_TARGET_LABELS,
            }
        ) or request.side_effect_policy is not EvidenceSideEffectPolicy.READ_ONLY:
            return baseline
        seconds = min(
            work.budget.remaining_duration_ms / 1000,
            (baseline.deadline - self._now()).total_seconds(),
        )
        if seconds <= 0 or work.budget.remaining_actions <= 0:
            return ExplorationTerminal(
                work_id=work.work_id, task_id=work.task_id,
                status=ExplorationTerminalStatus.BUDGET_EXHAUSTED,
                reason="H5 补采已到冻结截止或没有剩余动作，不再读取候选或调用模型。",
            )
        timeout = asyncio.timeout(max(seconds, 0))
        try:
            async with timeout:
                return await self._plan_within_budget(work=work, baseline=baseline)
        except TimeoutError:
            if not timeout.expired():
                raise
            return ExplorationTerminal(
                work_id=work.work_id,
                task_id=work.task_id,
                status=ExplorationTerminalStatus.BUDGET_EXHAUSTED,
                reason="H5 补采候选规划用尽本工作项的既有时间预算。",
            )

    async def _plan_within_budget(
        self,
        *,
        work: ProbeWork,
        baseline: ActionIntent,
    ) -> ActionIntent | ExplorationTerminal:
        request = work.evidence_request
        frontier = await self._frontiers.read_probe_frontier(work=work)
        if (
            frontier.observation.capture_id != work.source_capture_id
            or frontier.observation.surface_kind is not SurfaceKind.WEB
        ):
            raise ValueError("H5 补采候选与恢复后来源 Capture 不一致")
        from services.api.app.application.h5_recapture_coalescing import compatible_observation_key

        requirement = request.required_interaction
        if requirement is not None:
            if (
                frontier.observation.page_state != "stable"
                or frontier.observation.overlay_signature is not None
                or frontier.observation.metadata.get("requires_auth") is True
            ):
                return ExplorationTerminal(
                    work_id=work.work_id,
                    task_id=work.task_id,
                    status=ExplorationTerminalStatus.BLOCKED,
                    reason="H5 补证现场未稳定、被遮挡或需要认证，不能跳过恢复门禁。",
                )
            kind = requirement.parameters.get("goal_capability")
            frontier = await self._pair_candidates(work=work, frontier=frontier, kind=kind)
            candidates = tuple(
                candidate
                for candidate in frontier.candidates
                if (
                    candidate.action_type is requirement.operation
                    and candidate.risk_level is InteractionRiskLevel.LOW
                    and candidate.candidate_id not in frontier.blocked_candidate_ids
                    and candidate.navigation_depth <= work.budget.max_depth
                    and (
                        kind == "page_tab_navigation"
                        and (candidate.metadata.get("action_parameters") or {}).get("target_mode")
                        == kind
                        or kind == "cover_content_pair"
                        and candidate.metadata.get("probe_subject_key")
                        == requirement.parameters.get("probe_subject_key")
                        and bool(candidate.metadata.get("probe_subject_key"))
                        or kind == "payment_entry_pair"
                        and (candidate.metadata.get("action_parameters") or {}).get("target_mode")
                        == "profile_payment_navigation"
                        or kind == "payment_plan_reveal"
                        and (candidate.metadata.get("action_parameters") or {}).get("target_mode")
                        == "h5_payment_horizontal_collection"
                        or kind in PROBE_TARGET_LABELS
                        and (candidate.metadata.get("action_parameters") or {}).get("target_mode")
                        == PROBE_TARGET_MODE
                        or kind == "vertical_scroll"
                        and (
                            (candidate.metadata.get("action_parameters") or {}).get("target_mode")
                            == "h5_list_scroll_coverage"
                            or (
                                (candidate.metadata.get("action_parameters") or {}).get("direction")
                                == "down"
                                and (candidate.metadata.get("action_parameters") or {}).get(
                                    "axis", "vertical"
                                ) == "vertical"
                            )
                        )
                    )
                )
            )
            if not candidates:
                return ExplorationTerminal(
                    work_id=work.work_id,
                    task_id=work.task_id,
                    status=ExplorationTerminalStatus.BLOCKED,
                    reason="H5 补证动作合同没有对应的低风险现场候选，未调用模型或消耗动作。",
                )
            frontier = replace(frontier, candidates=candidates)

        if (
            compatible_observation_key(request) is not None
            and baseline.action_type is InteractionOperation.OBSERVE
            and frontier.observation.page_state == "stable"
            and frontier.observation.overlay_signature is None
            and frontier.observation.metadata.get("requires_auth") is not True
        ):
            return baseline
        # 稳定现场没有可点击入口，也应保留请求原本许可的零触碰观察选项。
        if (
            requirement is None
            and frontier.observation.page_state == "stable"
            and frontier.observation.page_type
            and frontier.observation.overlay_signature is None
            and frontier.observation.metadata.get("requires_auth") is not True
            and not any(
                candidate.action_type is InteractionOperation.OBSERVE
                for candidate in frontier.candidates
            )
        ):
            digest = hashlib.sha256(
                f"{work.work_id}:{frontier.snapshot_id}:{work.source_capture_id}".encode()
            ).hexdigest()
            frontier = replace(
                frontier,
                candidates=(
                    *frontier.candidates,
                    CandidateAction(
                        candidate_id=f"h5-probe-observe:{digest}",
                        semantic_target="重新观察当前补采目标页面",
                        action_type=InteractionOperation.OBSERVE,
                        risk_level=InteractionRiskLevel.LOW,
                        exploration_value=0,
                        navigation_depth=0,
                        expected_surface_kind=SurfaceKind.WEB,
                    ),
                ),
            )
        decision = await self._select_goal(
            request=request,
            frontier=frontier,
            budget=work.budget,
            deadline=baseline.deadline,
            session_id=work.session_id,
            branch_id=work.branch_id,
            baseline=baseline if requirement is not None else None,
        )
        if isinstance(decision, ExplorationTerminal):
            if decision.task_id != work.task_id:
                raise ValueError("H5 补采候选选择返回了其他任务的终态")
            return replace(decision, work_id=work.work_id)
        if not isinstance(decision, ActionIntent):
            raise ValueError("H5 补采候选选择没有返回有效动作")
        candidate = next(
            (
                item
                for item in frontier.candidates
                if item.candidate_id == decision.parameters.get("candidate_id")
            ),
            None,
        )
        if (
            candidate is None
            or decision.task_id != work.task_id
            or decision.session_id != work.session_id
            or decision.source_capture_id != work.source_capture_id
            or decision.action_type is not candidate.action_type
            or decision.semantic_target != candidate.semantic_target
            or candidate.risk_level is not InteractionRiskLevel.LOW
            or candidate.candidate_id in frontier.blocked_candidate_ids
            or candidate.navigation_depth > work.budget.max_depth
        ):
            raise ValueError("H5 补采选择未绑定当前可执行的低风险候选")
        # 执行参数只取现场签发的候选，模型只能选 ID，不能注入脚本或坐标。
        parameters = dict(decision.parameters)
        parameters.update(baseline.parameters)
        parameters.update(
            {
                "candidate_id": candidate.candidate_id,
                "frontier_snapshot_id": frontier.snapshot_id,
                "navigation_depth": candidate.navigation_depth,
            }
        )
        if candidate.metadata.get("destination_url"):
            parameters["destination_url"] = candidate.metadata["destination_url"]
        profile = CaptureProfilePolicy.for_surface(SurfaceKind.WEB, candidate.action_type)
        identity = hashlib.sha256(
            f"{baseline.idempotency_key}:{frontier.snapshot_id}:{candidate.candidate_id}".encode()
        ).hexdigest()
        return replace(
            baseline,
            intent_id=f"probe-intent:{identity}",
            idempotency_key=f"probe-intent:{identity}",
            action_type=candidate.action_type,
            semantic_target=candidate.semantic_target,
            # 证据目标保留在请求中，动作目的只描述候选，避免将说明文案误作授权操作。
            purpose=f"定向补采：{candidate.semantic_target}",
            parameters=parameters,
            capture_profile=CaptureProfile(
                profile_id=f"h5-probe-{candidate.action_type.value}-v1",
                before=CaptureDirective(
                    role="probe_before",
                    artifact_types=profile.before.artifact_types,
                    inherit_parent_semantics=True,
                ),
                after=CaptureDirective(
                    role="probe_result_replay",
                    artifact_types=tuple(
                        dict.fromkeys(
                            (
                                *baseline.capture_profile.after.artifact_types,
                                *profile.after.artifact_types,
                            )
                        )
                    ),
                ),
            ),
        )

    async def _pair_candidates(self, *, work: ProbeWork, frontier: FrontierSnapshot, kind):
        """配对动作只从恢复后的受信帧签发；缺原对象或读帧能力时保持缺口。"""
        if kind not in {"cover_content_pair", *PROBE_TARGET_LABELS}:
            return frontier
        if self._snapshot_reader is None:
            return replace(frontier, candidates=())
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=work.task_id,
            page_capture_id=work.source_capture_id,
        )
        page_type = frontier.observation.page_type or ""
        if kind == "cover_content_pair":
            matches = []
            expected = work.evidence_request.required_interaction.parameters.get(
                "probe_subject_key"
            )
            for candidate in frontier.candidates:
                raw = candidate.metadata.get("action_parameters") or {}
                if raw.get("target_mode") != "list_detail_navigation":
                    continue
                subject = select_cover_subject(
                    snapshot, page_type=page_type, selector=raw.get("list_detail_selector")
                )
                if subject is not None and expected == probe_subject_key(subject):
                    matches.append(
                        replace(
                            candidate,
                            metadata={**candidate.metadata, "probe_subject_key": expected},
                        )
                    )
            return replace(frontier, candidates=tuple(matches))
        expected = work.evidence_request.required_interaction.parameters.get("probe_subject_key")
        subject = select_probe_target(
            snapshot, goal=kind, page_type=page_type, expected_key=expected,
        )
        if subject is None or expected != probe_subject_key(subject):
            return replace(frontier, candidates=())
        parameters = {
            "target_mode": PROBE_TARGET_MODE,
            "goal_capability": kind,
            "source_page_type": page_type,
            "probe_subject_key": probe_subject_key(subject),
        }
        metadata = frontier.observation.metadata
        candidate = CandidateAction(
            candidate_id="h5-probe-target:"
            + stable_protocol_hash({"capture_id": work.source_capture_id, **parameters}),
            semantic_target=PROBE_TARGET_LABELS[kind],
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=1,
            navigation_depth=0,
            expected_surface_kind=SurfaceKind.WEB,
            metadata={
                "action_parameters": parameters,
                "source_page_type": page_type,
                "logical_target_required": metadata.get("logical_targets_effective") is True,
                "source_identity_fingerprint": metadata.get("logical_surface_identity_fingerprint"),
                "source_state_fingerprint": metadata.get("logical_surface_state_fingerprint"),
            },
        )
        return replace(frontier, candidates=(candidate,))

    async def _select_goal(
        self,
        *,
        request: EvidenceRequest,
        frontier: FrontierSnapshot,
        budget: ExplorationBudget,
        deadline: datetime,
        session_id: str,
        branch_id: str,
        baseline: ActionIntent | None = None,
    ) -> ActionIntent | ExplorationTerminal:
        metadata = dict(frontier.observation.metadata)
        # 主探索覆盖账本不裁决补证目标；仍保留现场候选的安全与身份约束。
        metadata.pop(H5_TASK_FRONTIER_METADATA_KEY, None)
        metadata["objective_satisfied"] = False
        frontier = replace(
            frontier,
            observation=replace(
                frontier.observation,
                metadata=metadata,
            ),
        )
        host = urlsplit(frontier.observation.location).hostname
        if not host:
            raise ValueError("H5 补采候选缺少已恢复页面的主机身份")
        selection = build_exploration_work(
            task_id=request.task_id,
            session_id=session_id,
            branch_id=branch_id,
            objective=(
                "本轮仅为定向补采选择一条现场候选，不做广泛探索。证据目标："
                + request.evidence_goal
                + "。仅选能补充该目标的候选；目标无需交互时选观察；"
                "没有相关安全候选时返回 blocked，不得用无关动作或观察冒充目标完成。"
            ),
            surface_kind=SurfaceKind.WEB,
            source_capture_id=frontier.observation.capture_id,
            frontier_snapshot_id=frontier.snapshot_id,
            frontier=frontier,
            boundaries=ExplorationBoundaries(
                allowed_hosts=(host,),
                allowed_operations=(
                    InteractionOperation.OBSERVE,
                    InteractionOperation.CLICK,
                    InteractionOperation.SCROLL,
                    InteractionOperation.TIMED_CAPTURE,
                    InteractionOperation.SEEK_MEDIA,
                ),
                max_navigation_depth=budget.max_depth,
                side_effect_policy=SideEffectPolicy.READ_ONLY,
            ),
            budget=budget,
            deadline=deadline,
        )
        try:
            if baseline is not None and len(frontier.candidates) == 1:
                candidate = frontier.candidates[0]
                decision = replace(
                    baseline,
                    action_type=candidate.action_type,
                    semantic_target=candidate.semantic_target,
                    parameters={"candidate_id": candidate.candidate_id},
                )
            else:
                decision = await self._delegate.decide_next(selection)
        except ExplorerPrematureTerminalError:
            # 主探索有候选必须继续；补采有无关候选不等于有可用补证动作。
            return ExplorationTerminal(
                work_id=selection.work_id,
                task_id=request.task_id,
                status=ExplorationTerminalStatus.BLOCKED,
                reason="H5 补采未能从当前候选中选择满足证据目标的安全动作。",
            )
        if isinstance(decision, ExplorationTerminal):
            if decision.task_id != request.task_id:
                raise ValueError("H5 补采候选选择返回了其他任务的终态")
            return decision
        if not isinstance(decision, ActionIntent):
            raise ValueError("H5 补采候选选择没有返回有效动作")
        candidate = next(
            (
                item
                for item in frontier.candidates
                if item.candidate_id == decision.parameters.get("candidate_id")
            ),
            None,
        )
        if (
            candidate is None
            or decision.task_id != request.task_id
            or decision.session_id != session_id
            or decision.source_capture_id != frontier.observation.capture_id
            or decision.action_type is not candidate.action_type
            or decision.semantic_target != candidate.semantic_target
            or candidate.risk_level is not InteractionRiskLevel.LOW
            or candidate.candidate_id in frontier.blocked_candidate_ids
            or candidate.action_type not in selection.boundaries.allowed_operations
            or candidate.navigation_depth > budget.max_depth
        ):
            raise ValueError("H5 补采选择未绑定当前可执行的低风险候选")
        parameters = dict(candidate.metadata.get("action_parameters") or {})
        from services.api.app.application.h5_runtime_composition import (
            _h5_candidate_parameters,
            _has_visual_navigation_provenance,
        )

        if (
            candidate.metadata.get("logical_target_required") is True
            or "target_mode" in parameters
            or _has_visual_navigation_provenance(
                candidate=candidate,
                action_parameters=parameters,
            )
        ):
            # 补采与主探索共用签发校验，不能丢掉新现场逻辑签名而退回旧解析。
            try:
                parameters = _h5_candidate_parameters(
                    work=selection,
                    candidate=candidate,
                    h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
                )
            except CaptureCoordinationError as error:
                raise EvidenceRecoveryReplayError(
                    reason_code=error.error_code,
                    message="H5 补采候选来源已失效",
                ) from error
        parameters.update(
            {
                "candidate_id": candidate.candidate_id,
                "frontier_snapshot_id": frontier.snapshot_id,
                "navigation_depth": candidate.navigation_depth,
            }
        )
        if candidate.metadata.get("probe_subject_key"):
            parameters["probe_subject_key"] = candidate.metadata["probe_subject_key"]
        if candidate.metadata.get("destination_url"):
            parameters["destination_url"] = candidate.metadata["destination_url"]
        return replace(decision, parameters=parameters)
