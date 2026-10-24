"""App 探索最终观察、证据封存与设备会话关闭编排。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from services.api.app.application.app_capture_persistence import PersistedAppCapture
from services.api.app.application.app_evidence_manifest import (
    AppEvidenceManifestFinalizer,
)
from services.api.app.application.app_exploration_cancellation import (
    AppExplorationCanceledError,
    run_app_exploration_operation,
)
from services.api.app.application.app_exploration_evidence import (
    AppEvidenceIndexRepository,
    AppExplorationEvidenceAssembler,
)
from services.api.app.application.app_exploration_reconciliation import (
    AppExplorationResultIntent,
    AppExplorationResultIntentRepository,
)
from services.api.app.application.app_exploration_results import (
    AppExplorationResultRepository,
)
from services.api.app.application.app_metrics import (
    APP_EXPLORATION_CLOSURE_FAILURE_TOTAL,
    APP_EXPLORATION_EVIDENCE_GAP_TOTAL,
    APP_EXPLORATION_VISUAL_FACT_MISSING_TOTAL,
    record_app_metric_safely,
)
from services.api.app.application.app_screenshot_exploration import (
    AppScreenshotUnderstandingPort,
    AppVisualObservationRepositoryPort,
    ensure_app_capture_visual_observation,
)
from services.api.app.application.app_screenshot_understanding import (
    AppScreenshotUnderstandingError,
)
from services.api.app.application.app_workflow import AppWorkflowCancellation
from services.api.app.application.device_lease_settlement import (
    preserves_cloud_phone_lease,
    settle_device_lease_after_session_loss,
)
from services.api.app.application.device_pool import (
    quarantine_device_lease_for_session,
    release_device_lease_for_session,
)
from services.api.app.application.evidence_manifests import (
    CaptureEvidenceManifest,
    EvidenceManifestRepository,
)
from services.api.app.application.page_graph_projection import (
    attach_app_coverage_page_graph_shadow,
)
from services.api.app.application.surface_sessions import (
    SessionStateConflictError,
    SurfaceSessionRepository,
)
from services.api.app.domain.evidence import (
    AppEvidenceGap,
    AppEvidenceIndex,
    AppEvidenceIndexStatus,
    AppEvidenceSemanticRole,
    AppExplorationResult,
    AppExplorationResultOutcome,
    EvidenceQualityStatus,
    app_exploration_result_context_sha256,
    seal_app_evidence_index,
)
from services.api.app.domain.exploration import ExplorationBudget
from services.api.app.domain.interaction import (
    CaptureDirective,
    CaptureRef,
    SurfaceDriver,
    SurfaceKind,
    SurfaceSessionRef,
    SurfaceSessionSnapshot,
    SurfaceSessionStatus,
    TimedObservationIntent,
)
from services.api.app.domain.protocol import freeze_json_mapping, require_aware_datetime

logger = logging.getLogger(__name__)


class AppExplorationClosureError(RuntimeError):
    """最终证据或 Session 排空条件不足，关闭流程不能继续。"""


class _ObservationCoordinator(Protocol):
    async def capture_observation(
        self,
        *,
        agent_run_id: str,
        intent: TimedObservationIntent,
        allow_draining: bool = False,
        semantic_role: str = "surface_reobserve",
    ) -> CaptureRef: ...


class _AppCaptureReader(Protocol):
    async def get_capture(self, *, capture_id: str) -> PersistedAppCapture | None: ...


class _ActiveSessionInspectionDrainer(Protocol):
    """在 App Session 仍存活时完成规则发布与判定排空。"""

    async def drain_before_close(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        deadline: datetime,
    ) -> object: ...


class _SupplementalEvidenceDrainer(Protocol):
    async def drain(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        budget: ExplorationBudget,
        deadline: datetime,
    ) -> object: ...


@dataclass(frozen=True, kw_only=True)
class CloseAppExplorationCommand:
    """从领域终止进入最终观察和封存所需的稳定输入。"""

    task_id: str
    agent_run_id: str
    session_id: str
    build_id: str
    surface_kind: SurfaceKind
    outcome: AppExplorationResultOutcome
    terminal_reason_code: str
    coverage_summary: Mapping[str, Any]
    started_at: datetime
    deadline: datetime
    capture_final_observation: bool = True
    run_evidence_gap_reason_code: str | None = None
    supplemental_evidence_budget: ExplorationBudget | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "agent_run_id",
            "session_id",
            "build_id",
            "terminal_reason_code",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
        if self.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
            raise ValueError("App 探索关闭只支持 Android/iOS")
        if not isinstance(self.outcome, AppExplorationResultOutcome):
            raise ValueError("outcome 不受支持")
        if not isinstance(self.capture_final_observation, bool):
            raise ValueError("capture_final_observation 必须为布尔值")
        if self.supplemental_evidence_budget is not None and not isinstance(
            self.supplemental_evidence_budget,
            ExplorationBudget,
        ):
            raise ValueError("supplemental_evidence_budget 必须为 ExplorationBudget")
        if self.run_evidence_gap_reason_code is not None and (
            not isinstance(self.run_evidence_gap_reason_code, str)
            or not self.run_evidence_gap_reason_code.strip()
            or self.run_evidence_gap_reason_code != self.run_evidence_gap_reason_code.strip()
        ):
            raise ValueError("run_evidence_gap_reason_code 必须为非空无首尾空白字符串")
        require_aware_datetime(self.started_at, "started_at")
        require_aware_datetime(self.deadline, "deadline")
        if self.deadline <= self.started_at:
            raise ValueError("deadline 必须晚于 started_at")
        object.__setattr__(
            self,
            "coverage_summary",
            freeze_json_mapping(self.coverage_summary, "coverage_summary"),
        )


@dataclass(frozen=True, kw_only=True)
class AppExplorationClosureResult:
    """已封存证据和最终 Session 状态；close_error 不撤销证据。"""

    final_observation: CaptureRef | None
    index: AppEvidenceIndex
    exploration_result: AppExplorationResult
    session: SurfaceSessionSnapshot
    close_error: str | None = None


class _PageGraphShadowLoader(Protocol):
    async def __call__(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> Mapping[str, Any] | None: ...


class AppExplorationClosureService:
    """严格执行 Final Observation → Seal → Session Closure。"""

    def __init__(
        self,
        *,
        capture_coordinator: _ObservationCoordinator,
        captures: _AppCaptureReader,
        manifests: EvidenceManifestRepository,
        manifest_finalizer: AppEvidenceManifestFinalizer,
        evidence_assembler: AppExplorationEvidenceAssembler,
        evidence_indexes: AppEvidenceIndexRepository,
        exploration_results: AppExplorationResultRepository,
        result_intents: AppExplorationResultIntentRepository | None = None,
        understanding: AppScreenshotUnderstandingPort,
        visual_observations: AppVisualObservationRepositoryPort,
        sessions: SurfaceSessionRepository,
        driver: SurfaceDriver,
        active_session_inspection: _ActiveSessionInspectionDrainer | None = None,
        supplemental_evidence_drain: _SupplementalEvidenceDrainer | None = None,
        cancellation: AppWorkflowCancellation | None = None,
        cancellation_poll_interval_seconds: float = 1.0,
        now: Callable[[], datetime] | None = None,
        page_graph_shadow_loader: _PageGraphShadowLoader | None = None,
    ) -> None:
        if cancellation_poll_interval_seconds <= 0:
            raise ValueError("取消轮询间隔必须大于 0")
        self._capture_coordinator = capture_coordinator
        self._captures = captures
        self._manifests = manifests
        self._manifest_finalizer = manifest_finalizer
        self._evidence_assembler = evidence_assembler
        self._evidence_indexes = evidence_indexes
        self._exploration_results = exploration_results
        self._result_intents = result_intents
        self._understanding = understanding
        self._visual_observations = visual_observations
        self._sessions = sessions
        self._driver = driver
        self._active_session_inspection = active_session_inspection
        self._supplemental_evidence_drain = supplemental_evidence_drain
        self._cancellation = cancellation
        self._cancellation_poll_interval_seconds = cancellation_poll_interval_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._page_graph_shadow_loader = page_graph_shadow_loader

    async def _coverage_summary_with_page_graph_shadow(
        self,
        command: CloseAppExplorationCommand,
    ) -> Mapping[str, Any]:
        """探索收口写入影子对账；恒不改 Coverage／Gate 权威。"""

        graph: Mapping[str, Any] | None = None
        if self._page_graph_shadow_loader is not None:
            try:
                graph = await self._page_graph_shadow_loader(
                    task_id=command.task_id,
                    agent_run_id=command.agent_run_id,
                )
            except Exception:  # noqa: BLE001 - 影子失败不得阻断封存
                logger.exception(
                    "App Coverage↔图影子对账加载失败 task_id=%s run_id=%s",
                    command.task_id,
                    command.agent_run_id,
                )
                graph = None
        return attach_app_coverage_page_graph_shadow(
            dict(command.coverage_summary),
            graph=dict(graph) if isinstance(graph, Mapping) else None,
        )

    async def close(
        self,
        command: CloseAppExplorationCommand,
    ) -> AppExplorationClosureResult:
        if not isinstance(command, CloseAppExplorationCommand):
            raise TypeError("command 必须为 CloseAppExplorationCommand")
        try:
            async with self._evidence_indexes.closure_lock(run_id=command.agent_run_id):
                try:
                    return await self._close(command)
                except asyncio.CancelledError as exc:
                    should_record, recovered = await asyncio.shield(
                        self._mark_lost_after_closure_failure(
                            command=command,
                            error=exc,
                        )
                    )
                    if should_record:
                        record_app_metric_safely(
                            APP_EXPLORATION_CLOSURE_FAILURE_TOTAL,
                            phase="closure",
                            error_type=type(exc).__name__,
                            recovered=str(recovered).lower(),
                        )
                    exc.app_closure_cleanup_handled = True
                    raise
                except Exception as exc:
                    should_record, recovered = await self._mark_lost_after_closure_failure(
                        command=command,
                        error=exc,
                    )
                    if should_record:
                        record_app_metric_safely(
                            APP_EXPLORATION_CLOSURE_FAILURE_TOTAL,
                            phase="closure",
                            error_type=type(exc).__name__,
                            recovered=str(recovered).lower(),
                        )
                    raise
        except asyncio.CancelledError as exc:
            # Runtime 据此避免在等待另一 Closure 的锁时误伤赢家 Session。
            exc.app_closure_cleanup_handled = True
            raise

    async def _close(
        self,
        command: CloseAppExplorationCommand,
    ) -> AppExplorationClosureResult:
        result_context_sha256 = app_exploration_result_context_sha256(
            coverage_summary=command.coverage_summary,
            started_at=command.started_at,
        )
        if self._result_intents is not None:
            await self._result_intents.record(
                intent=AppExplorationResultIntent(
                    task_id=command.task_id,
                    agent_run_id=command.agent_run_id,
                    surface_session_id=command.session_id,
                    result_context_sha256=result_context_sha256,
                    coverage_summary=command.coverage_summary,
                    started_at=command.started_at,
                    created_at=self._now(),
                )
            )
        existing = await self._evidence_indexes.get_by_run_id(run_id=command.agent_run_id)
        existing_result = await self._exploration_results.get_by_run_id(run_id=command.agent_run_id)
        final_observation: CaptureRef | None = None
        if existing is not None and existing.status is AppEvidenceIndexStatus.SEALED:
            sealed = _validate_existing_index(existing, command=command)
            await self._redrain_active_session_before_sealed_replay(command)
        else:
            capture_gaps: tuple[AppEvidenceGap, ...] = (
                (
                    AppEvidenceGap(
                        semantic_role=AppEvidenceSemanticRole.ACTION_AFTER,
                        reason_code=command.run_evidence_gap_reason_code,
                        description="App 动作恢复缺少可确认的执行或 After Capture 事实",
                        metadata={"scope": "run"},
                    ),
                )
                if command.run_evidence_gap_reason_code is not None
                else ()
            )
            if not command.capture_final_observation:
                final_gap_reason_code = (
                    command.run_evidence_gap_reason_code
                    or "workflow_canceled_before_final_observation"
                )
                final_gap_description = (
                    "App Runtime 无法恢复，未发起新的最终观察，按证据不足封存已有证据"
                    if command.run_evidence_gap_reason_code is not None
                    else "探索已取消，未发起新的最终观察，按证据不足封存已有证据"
                )
                capture_gaps = (
                    *capture_gaps,
                    AppEvidenceGap(
                        semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
                        reason_code=final_gap_reason_code,
                        description=final_gap_description,
                        metadata={"scope": "run"},
                    ),
                )
            elif self._now() >= command.deadline:
                capture_gaps = (
                    *capture_gaps,
                    AppEvidenceGap(
                        semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
                        reason_code="final_observation_deadline_exhausted",
                        description="探索硬截止已到，不再发起新的设备采集，按证据不足封存已有证据",
                        metadata={
                            "scope": "run",
                            "deadline": command.deadline.isoformat(),
                            "observed_at": self._now().isoformat(),
                        },
                    ),
                )
            else:
                session = await self._require_session(command)
                try:
                    final_observation = await run_app_exploration_operation(
                        self._capture_final_observation(command, session=session),
                        cancellation=self._cancellation,
                        task_id=command.task_id,
                        agent_run_id=command.agent_run_id,
                        poll_interval_seconds=self._cancellation_poll_interval_seconds,
                    )
                except Exception as exc:  # noqa: BLE001 - 最终观察失败也必须封存已有证据
                    canceled = isinstance(exc, AppExplorationCanceledError)
                    capture_gaps = (
                        *capture_gaps,
                        AppEvidenceGap(
                            semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
                            reason_code=(
                                "workflow_canceled_during_final_observation"
                                if canceled
                                else "final_observation_capture_failed"
                            ),
                            description=(
                                "最终观察期间收到取消信号，已停止采集并封存已有探索证据"
                                if canceled
                                else "最终观察采集失败，已按证据不足封存已有探索证据"
                            ),
                            metadata={
                                "scope": "run",
                                "error_type": type(exc).__name__,
                            },
                        ),
                    )
            captures, finalization_gaps = await self._finalize_manifests(command)
            (
                visual_gaps,
                invalid_visual_capture_ids,
            ) = await self._ensure_pass_capture_visual_observations(
                command=command,
                captures=captures,
            )
            if (
                self._supplemental_evidence_drain is not None
                and command.supplemental_evidence_budget is not None
            ):
                await self._drain_supplemental_evidence(command)
                captures, additional_finalization_gaps = await self._finalize_manifests(command)
                additional_visual_gaps, additional_invalid_capture_ids = (
                    await self._ensure_pass_capture_visual_observations(
                        command=command,
                        captures=captures,
                    )
                )
                finalization_gaps = _deduplicate_gaps(
                    (*finalization_gaps, *additional_finalization_gaps)
                )
                visual_gaps = _deduplicate_gaps((*visual_gaps, *additional_visual_gaps))
                invalid_visual_capture_ids = frozenset(
                    (*invalid_visual_capture_ids, *additional_invalid_capture_ids)
                )
            elif self._active_session_inspection is not None:
                await self._active_session_inspection.drain_before_close(
                    task_id=command.task_id,
                    agent_run_id=command.agent_run_id,
                    deadline=command.deadline,
                )
            indexable_captures = tuple(
                item for item in captures if item.capture_id not in invalid_visual_capture_ids
            )
            assembly = self._evidence_assembler.assemble(
                task_id=command.task_id,
                run_id=command.agent_run_id,
                build_id=command.build_id,
                surface_session_id=command.session_id,
                captures=indexable_captures,
                created_at=self._now(),
                result_context_sha256=result_context_sha256,
            )
            gaps = _deduplicate_gaps(
                (*capture_gaps, *finalization_gaps, *visual_gaps, *assembly.gaps)
            )
            has_final_entry = any(
                entry.semantic_role is AppEvidenceSemanticRole.FINAL_OBSERVATION
                for entry in assembly.index.entries
            )
            if not has_final_entry and not any(
                gap.semantic_role is AppEvidenceSemanticRole.FINAL_OBSERVATION for gap in gaps
            ):
                gaps = (
                    *gaps,
                    AppEvidenceGap(
                        semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
                        reason_code="final_observation_missing",
                        description="最终观察未形成 PASS Evidence Index Entry",
                        metadata={"scope": "run"},
                    ),
                )
            building = (await self._evidence_indexes.create(index=assembly.index)).index
            sealed_at = self._now()
            outcome, terminal_reason_code = _sealed_outcome(
                command=command,
                gaps=gaps,
                has_final_entry=has_final_entry,
            )
            sealed = seal_app_evidence_index(
                building,
                outcome=outcome,
                terminal_reason_code=terminal_reason_code,
                evidence_gaps=gaps,
                sealed_at=sealed_at,
            )
            sealed = (await self._evidence_indexes.seal(index=sealed)).index
            _record_sealed_evidence_metrics(sealed)

        if existing_result is not None:
            _validate_existing_result(existing_result, index=sealed, command=command)
            exploration_result = existing_result
        else:
            if sealed.result_context_sha256 != result_context_sha256:
                raise AppExplorationClosureError("sealed Evidence Index 缺少或不匹配探索结果上下文")
            completed_at = sealed.sealed_at or self._now()
            coverage_summary = await self._coverage_summary_with_page_graph_shadow(command)
            exploration_result = AppExplorationResult(
                task_id=command.task_id,
                run_id=command.agent_run_id,
                surface_session_id=command.session_id,
                outcome=sealed.outcome or AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
                terminal_reason_code=sealed.terminal_reason_code or "evidence_incomplete",
                evidence_index_id=sealed.index_id,
                evidence_index_hash=sealed.content_sha256 or "",
                coverage_summary=coverage_summary,
                evidence_gaps=sealed.evidence_gaps,
                started_at=command.started_at,
                completed_at=completed_at,
            )
            exploration_result = (
                await self._exploration_results.create(result=exploration_result)
            ).result
        try:
            session, close_error = await self._close_session(
                command,
                reason=command.terminal_reason_code,
            )
        except Exception as exc:  # noqa: BLE001 - 已封存结果仍需交接给下游复核
            try:
                lost = await self.mark_lost(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    reason=f"app_exploration_session_close_failed:{type(exc).__name__}",
                    expected_surface_kind=command.surface_kind,
                    expected_build_id=command.build_id,
                )
            except Exception as lost_error:  # noqa: BLE001 - 保留原始关闭异常
                raise exc from lost_error
            if (
                lost is None
                or lost.session_id != command.session_id
                or lost.task_id != command.task_id
                or lost.surface_kind is not command.surface_kind
                or lost.platform_build_ref != command.build_id
            ):
                raise
            session = lost
            if lost.status is SurfaceSessionStatus.CLOSED:
                # mark_closed 可能已提交但客户端在收到响应前断链；权威终态优先。
                close_error = None
            elif lost.status in {
                SurfaceSessionStatus.LOST,
                SurfaceSessionStatus.FAILED,
            }:
                close_error = str(exc)
                record_app_metric_safely(
                    APP_EXPLORATION_CLOSURE_FAILURE_TOTAL,
                    phase="session_close",
                    error_type=type(exc).__name__,
                    recovered="true",
                )
            else:
                raise
        device_close_error = await self._settle_device_lease(
            session=session,
            reason=command.terminal_reason_code,
        )
        if close_error is None and device_close_error is not None:
            close_error = device_close_error
        return AppExplorationClosureResult(
            final_observation=final_observation,
            index=sealed,
            exploration_result=exploration_result,
            session=session,
            close_error=close_error,
        )

    async def _redrain_active_session_before_sealed_replay(
        self,
        command: CloseAppExplorationCommand,
    ) -> None:
        """旧 sealed Index 若 Session 仍存活，关闭前必须重新验证规则排空。"""

        if self._active_session_inspection is None:
            return
        session = await self._sessions.get(session_id=command.session_id)
        if (
            session is None
            or session.task_id != command.task_id
            or session.surface_kind is not command.surface_kind
            or session.platform_build_ref != command.build_id
            or session.status.is_terminal
        ):
            return
        await self._active_session_inspection.drain_before_close(
            task_id=command.task_id,
            agent_run_id=command.agent_run_id,
            deadline=command.deadline,
        )

    async def _drain_supplemental_evidence(
        self,
        command: CloseAppExplorationCommand,
    ) -> None:
        drainer = self._supplemental_evidence_drain
        budget = command.supplemental_evidence_budget
        if drainer is None or budget is None:
            return
        session = await self._require_session(command)
        await drainer.drain(
            task_id=command.task_id,
            agent_run_id=command.agent_run_id,
            finalization_epoch=session.finalization_epoch,
            budget=budget,
            deadline=command.deadline,
        )

    async def mark_lost(
        self,
        *,
        task_id: str,
        session_id: str,
        reason: str,
        expected_surface_kind: SurfaceKind | None = None,
        expected_build_id: str | None = None,
    ) -> SurfaceSessionSnapshot | None:
        """技术失败时尽力把仍活跃的 Session 标记为 LOST。"""
        snapshot = await self._sessions.get(session_id=session_id)
        if (
            snapshot is None
            or snapshot.task_id != task_id
            or snapshot.session_id != session_id
            or (
                expected_surface_kind is not None
                and snapshot.surface_kind is not expected_surface_kind
            )
            or (expected_build_id is not None and snapshot.platform_build_ref != expected_build_id)
            or snapshot.status.is_terminal
        ):
            return snapshot
        # LOST 路径常跳过正规 close：这里尽力关掉 Driver／force-stop，
        # 避免同机下一颗 APK 撞上仍在前台的第一颗。
        if snapshot.current_surface_ref is not None:
            try:
                await self._driver.close_session(
                    session=SurfaceSessionRef(
                        task_id=snapshot.task_id,
                        session_id=snapshot.session_id,
                        surface_kind=snapshot.surface_kind,
                        primary_surface_ref=snapshot.current_surface_ref,
                    ),
                    reason=f"lost_apk_cleanup:{reason}",
                )
            except Exception:  # noqa: BLE001 - 清理失败不得阻止 LOST 落库
                logger.info(
                    "App Session LOST 前 Driver close／APK 清理跳过",
                    extra={
                        "task_id": task_id,
                        "session_id": session_id,
                        "reason": reason,
                    },
                    exc_info=True,
                )
        conflict: SessionStateConflictError | None = None
        # Driver 关闭期间仍可能有在途 Capture 提交；只重试状态 CAS，不重关真实设备。
        for attempt in range(4):
            current = await self._sessions.get(session_id=session_id)
            if (
                current is None
                or current.task_id != snapshot.task_id
                or current.session_id != snapshot.session_id
                or current.surface_kind is not snapshot.surface_kind
                or current.platform_build_ref != snapshot.platform_build_ref
                or current.finalization_epoch != snapshot.finalization_epoch
                or current.device_ref != snapshot.device_ref
                or current.device_lease_id != snapshot.device_lease_id
                or current.device_lease_fencing_token != snapshot.device_lease_fencing_token
                or current.status.is_terminal
                or current.current_surface_ref != snapshot.current_surface_ref
            ):
                return current
            # 第三次竞争后仍重读一次，允许并发提交终态幂等返回，但不发第四次 CAS。
            if attempt == 3:
                assert conflict is not None
                raise conflict
            try:
                lost = await self._sessions.mark_lost(
                    task_id=task_id,
                    session_id=session_id,
                    expected_row_version=current.row_version,
                    reason=reason,
                )
            except SessionStateConflictError as exc:
                conflict = exc
                continue
            if lost.device_lease_id is not None:
                # Official 502／TIMEOUT 等暂态截图故障：释放租约，禁止 quarantine 毒死整池。
                await settle_device_lease_after_session_loss(
                    session_id=session_id,
                    reason=reason,
                )
            return lost
        raise AssertionError("Session LOST CAS 重试未按边界退出")

    async def _settle_device_lease(
        self,
        *,
        session: SurfaceSessionSnapshot,
        reason: str,
    ) -> str | None:
        """关闭确认后才释放设备；任何不确定结果都转隔离并交给恢复机。"""

        if session.device_lease_id is None:
            return None
        if preserves_cloud_phone_lease(reason):
            return None
        if session.status is SurfaceSessionStatus.CLOSED:
            released = await release_device_lease_for_session(session_id=session.session_id)
            if released:
                return None
            await quarantine_device_lease_for_session(
                session_id=session.session_id,
                reason=f"app_session_release_unconfirmed:{reason}",
            )
            return "app_device_lease_release_unconfirmed"
        await quarantine_device_lease_for_session(
            session_id=session.session_id,
            reason=f"app_session_terminal_not_closed:{session.status.value}",
        )
        return f"app_device_lease_{session.status.value}"

    async def _mark_lost_after_closure_failure(
        self,
        *,
        command: CloseAppExplorationCommand,
        error: BaseException,
    ) -> tuple[bool, bool]:
        """仅为首次活跃 Session 失败落 LOST，并返回是否应计数及是否恢复。"""

        try:
            current = await self._sessions.get(session_id=command.session_id)
        except Exception:  # noqa: BLE001 - 读取失败仍是首次技术事故候选
            logger.exception(
                "App 探索 Closure 异常后读取 Session 失败",
                extra={
                    "task_id": command.task_id,
                    "agent_run_id": command.agent_run_id,
                    "session_id": command.session_id,
                },
            )
            return True, False
        if (
            current is None
            or current.task_id != command.task_id
            or current.surface_kind is not command.surface_kind
            or current.platform_build_ref != command.build_id
            or current.status.is_terminal
        ):
            # 错误归属或已终态重放不是新的 Closure 技术事故。
            return False, False
        try:
            snapshot = await self.mark_lost(
                task_id=command.task_id,
                session_id=command.session_id,
                reason=f"app_exploration_closure_failed:{type(error).__name__}",
                expected_surface_kind=command.surface_kind,
                expected_build_id=command.build_id,
            )
        except Exception:  # noqa: BLE001 - LOST 写入失败不得覆盖原始 Closure 异常
            logger.exception(
                "App 探索 Closure 异常后标记 Session LOST 失败",
                extra={
                    "task_id": command.task_id,
                    "agent_run_id": command.agent_run_id,
                    "session_id": command.session_id,
                },
            )
            return True, False
        return True, snapshot is not None and snapshot.status.is_terminal

    async def _require_session(
        self,
        command: CloseAppExplorationCommand,
    ) -> SurfaceSessionSnapshot:
        session = await self._sessions.get(session_id=command.session_id)
        if (
            session is None
            or session.session_id != command.session_id
            or session.task_id != command.task_id
            or session.surface_kind is not command.surface_kind
            or session.platform_build_ref != command.build_id
        ):
            raise AppExplorationClosureError("App Surface Session 不存在或归属不一致")
        if session.status is not SurfaceSessionStatus.ACTIVE:
            raise AppExplorationClosureError("最终观察要求 Session 仍处于 ACTIVE")
        if session.current_capture_id is None:
            raise AppExplorationClosureError("最终观察缺少当前 Capture 游标")
        return session

    async def _capture_final_observation(
        self,
        command: CloseAppExplorationCommand,
        *,
        session: SurfaceSessionSnapshot,
    ) -> CaptureRef:
        intent = TimedObservationIntent(
            task_id=command.task_id,
            session_id=command.session_id,
            phase_run_id=f"app-exploration-closure:{command.agent_run_id}",
            decision_id="final-observation",
            observation_id=f"final-observation:{command.agent_run_id}",
            source_capture_id=session.current_capture_id or "",
            surface_kind=command.surface_kind,
            delay_ms=0,
            directive=CaptureDirective(
                role="observation",
                artifact_types=(
                    "screenshot",
                    "ui_tree",
                    "ui_snapshot",
                    "visible_text",
                ),
            ),
            deadline=command.deadline,
        )
        return await self._capture_coordinator.capture_observation(
            agent_run_id=command.agent_run_id,
            intent=intent,
            semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION.value,
        )

    async def _finalize_manifests(
        self,
        command: CloseAppExplorationCommand,
    ) -> tuple[tuple[CaptureEvidenceManifest, ...], tuple[AppEvidenceGap, ...]]:
        captures = await self._manifests.list_latest_for_session(
            task_id=command.task_id,
            session_id=command.session_id,
            agent_run_id=command.agent_run_id,
        )
        gaps: list[AppEvidenceGap] = []
        for item in captures:
            manifest = item.manifest
            if manifest is not None and manifest.quality_status is EvidenceQualityStatus.PENDING:
                finalization = await self._manifest_finalizer.finalize(manifest)
                gaps.extend(finalization.gaps)
        finalized = await self._manifests.list_latest_for_session(
            task_id=command.task_id,
            session_id=command.session_id,
            agent_run_id=command.agent_run_id,
        )
        return finalized, tuple(gaps)

    async def _ensure_pass_capture_visual_observations(
        self,
        *,
        command: CloseAppExplorationCommand,
        captures: tuple[CaptureEvidenceManifest, ...],
    ) -> tuple[tuple[AppEvidenceGap, ...], frozenset[str]]:
        gaps: list[AppEvidenceGap] = []
        invalid_capture_ids: set[str] = set()
        processed_capture_ids: set[str] = set()
        for item in captures:
            manifest = item.manifest
            if (
                item.capture_id in processed_capture_ids
                or manifest is None
                or manifest.quality_status is not EvidenceQualityStatus.PASS
            ):
                continue
            processed_capture_ids.add(item.capture_id)
            role = _capture_semantic_role(item)
            try:
                await run_app_exploration_operation(
                    self._ensure_capture_visual_observation(
                        command=command,
                        item=item,
                    ),
                    cancellation=self._cancellation,
                    task_id=command.task_id,
                    agent_run_id=command.agent_run_id,
                    poll_interval_seconds=self._cancellation_poll_interval_seconds,
                )
            except AppExplorationCanceledError as exc:
                # 取消只应阻止新的视觉补采，不得中断既有证据的 fail-closed
                # 封存和 Driver 关闭。缺少视觉事实的 Capture 从索引排除并记 Gap。
                invalid_capture_ids.add(item.capture_id)
                gaps.append(
                    AppEvidenceGap(
                        semantic_role=role,
                        reason_code="workflow_canceled_before_visual_understanding",
                        description="探索已取消，未继续补齐 Capture 视觉事实，已按证据不足封存",
                        capture_id=item.capture_id,
                        metadata={
                            "scope": "closure",
                            "error_type": type(exc).__name__,
                        },
                    )
                )
            except AppScreenshotUnderstandingError as exc:
                if exc.error_code not in {
                    "SCREENSHOT_ARTIFACT_MISSING",
                    "SCREENSHOT_CHECKSUM_MISMATCH",
                }:
                    raise
                invalid_capture_ids.add(item.capture_id)
                metadata: dict[str, Any] = {"error_type": type(exc).__name__}
                error_code = getattr(exc, "error_code", None)
                if isinstance(error_code, str) and error_code.strip():
                    metadata["error_code"] = error_code
                is_final = role is AppEvidenceSemanticRole.FINAL_OBSERVATION
                gaps.append(
                    AppEvidenceGap(
                        semantic_role=role,
                        reason_code=(
                            "final_observation_visual_understanding_failed"
                            if is_final
                            else "capture_visual_understanding_failed"
                        ),
                        description=(
                            "最终观察缺少可信视觉事实，已按证据不足封存"
                            if is_final
                            else "Capture 缺少可信视觉事实，已按证据不足封存"
                        ),
                        capture_id=item.capture_id,
                        metadata=metadata,
                    )
                )
        return tuple(gaps), frozenset(invalid_capture_ids)

    async def _ensure_capture_visual_observation(
        self,
        *,
        command: CloseAppExplorationCommand,
        item: CaptureEvidenceManifest,
    ) -> None:
        manifest = item.manifest
        if manifest is None or manifest.quality_status is not EvidenceQualityStatus.PASS:
            raise AppExplorationClosureError("只有 PASS Manifest 可以补齐视觉事实")
        persisted = await self._captures.get_capture(capture_id=item.capture_id)
        if persisted is None:
            raise AppExplorationClosureError("PASS Capture 不存在或无法读取")
        capture = persisted.capture
        if (
            item.agent_run_id != command.agent_run_id
            or persisted.agent_run_id != command.agent_run_id
            or capture.task_id != command.task_id
            or capture.session_id != command.session_id
            or capture.capture_id != item.capture_id
            or capture.surface_kind is not command.surface_kind
            or capture.role != item.capture_role
            or manifest.task_id != command.task_id
            or manifest.session_id != command.session_id
            or manifest.capture_id != item.capture_id
        ):
            raise AppExplorationClosureError("Capture、Manifest 与关闭请求归属不一致")
        saved = await ensure_app_capture_visual_observation(
            capture=capture,
            understanding=self._understanding,
            observations=self._visual_observations,
            mismatch_message="App 视觉事实与 Evidence Capture 归属不一致",
        )
        screenshot_artifact_ids = {
            artifact.artifact_id
            for artifact in manifest.artifacts
            if artifact.artifact_type == "screenshot"
        }
        if saved.analysis.screenshot_artifact_id not in screenshot_artifact_ids:
            raise AppExplorationClosureError("App 视觉事实未绑定 PASS Manifest Screenshot")

    async def _close_session(
        self,
        command: CloseAppExplorationCommand,
        *,
        reason: str,
    ) -> tuple[SurfaceSessionSnapshot, str | None]:
        session = await self._sessions.get(session_id=command.session_id)
        if (
            session is None
            or session.session_id != command.session_id
            or session.task_id != command.task_id
            or session.surface_kind is not command.surface_kind
            or session.platform_build_ref != command.build_id
        ):
            raise AppExplorationClosureError("封存后 Surface Session 不存在或归属不一致")
        if session.status.is_terminal:
            return (
                session,
                None
                if session.status is SurfaceSessionStatus.CLOSED
                else f"surface_session_{session.status.value}",
            )
        if session.status is SurfaceSessionStatus.ACTIVE:
            session = await self._sessions.begin_draining(
                task_id=command.task_id,
                session_id=command.session_id,
                expected_row_version=session.row_version,
            )
        if session.status is SurfaceSessionStatus.DRAINING:
            readiness = await self._sessions.close_readiness(
                task_id=command.task_id,
                session_id=command.session_id,
            )
            if not readiness.ready:
                raise AppExplorationClosureError("Surface Session 尚未排空，禁止关闭 Driver")
            session = await self._sessions.seal_for_close(
                task_id=command.task_id,
                session_id=command.session_id,
                expected_row_version=session.row_version,
            )
        if session.status is not SurfaceSessionStatus.CLOSING:
            raise AppExplorationClosureError("Surface Session 未进入 CLOSING")
        if session.current_surface_ref is None:
            raise AppExplorationClosureError("Surface Session 缺少主 Surface 引用")
        await self._driver.close_session(
            session=SurfaceSessionRef(
                task_id=command.task_id,
                session_id=command.session_id,
                surface_kind=command.surface_kind,
                primary_surface_ref=session.current_surface_ref,
            ),
            reason=reason,
        )
        closed = await self._sessions.mark_closed(
            task_id=command.task_id,
            session_id=command.session_id,
            expected_row_version=session.row_version,
        )
        return closed, None


def _validate_existing_index(
    index: AppEvidenceIndex,
    *,
    command: CloseAppExplorationCommand,
) -> AppEvidenceIndex:
    if (
        index.task_id != command.task_id
        or index.run_id != command.agent_run_id
        or index.build_id != command.build_id
        or index.surface_session_id != command.session_id
        or index.content_sha256 != index.recompute_content_sha256()
        or not _existing_outcome_matches_command(index=index, command=command)
    ):
        raise AppExplorationClosureError("已有 sealed Evidence Index 与关闭请求不一致")
    return index


def _coverage_without_page_graph_shadow(coverage: Mapping[str, Any]) -> dict[str, Any]:
    """Gate／上下文哈希权威字段；影子对账栏不参与一致性比对。"""

    payload = dict(coverage)
    payload.pop("app_coverage_page_graph_shadow", None)
    payload.pop("app_page_graph_snapshot", None)
    return payload


def _validate_existing_result(
    result: AppExplorationResult,
    *,
    index: AppEvidenceIndex,
    command: CloseAppExplorationCommand,
) -> None:
    if (
        result.task_id != command.task_id
        or result.run_id != command.agent_run_id
        or result.surface_session_id != command.session_id
        or result.outcome is not index.outcome
        or result.terminal_reason_code != index.terminal_reason_code
        or result.evidence_index_id != index.index_id
        or result.evidence_index_hash != index.content_sha256
        or result.evidence_gaps != index.evidence_gaps
        or _coverage_without_page_graph_shadow(result.coverage_summary)
        != _coverage_without_page_graph_shadow(command.coverage_summary)
        or result.started_at != command.started_at
    ):
        raise AppExplorationClosureError("已有 App Exploration Result 与封存请求不一致")
    if (
        index.result_context_sha256 is not None
        and index.result_context_sha256
        != app_exploration_result_context_sha256(
            coverage_summary=_coverage_without_page_graph_shadow(result.coverage_summary),
            started_at=result.started_at,
        )
    ):
        raise AppExplorationClosureError("已有 App Exploration Result 上下文摘要不一致")


def _sealed_outcome(
    *,
    command: CloseAppExplorationCommand,
    gaps: tuple[AppEvidenceGap, ...],
    has_final_entry: bool,
) -> tuple[AppExplorationResultOutcome, str]:
    if gaps or not has_final_entry:
        return AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT, "evidence_incomplete"
    return command.outcome, command.terminal_reason_code


def _existing_outcome_matches_command(
    *,
    index: AppEvidenceIndex,
    command: CloseAppExplorationCommand,
) -> bool:
    if (
        index.outcome is command.outcome
        and index.terminal_reason_code == command.terminal_reason_code
    ):
        return True
    return (
        index.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
        and index.terminal_reason_code == "evidence_incomplete"
        and bool(index.evidence_gaps)
    )


def _deduplicate_gaps(gaps: tuple[AppEvidenceGap, ...]) -> tuple[AppEvidenceGap, ...]:
    result: list[AppEvidenceGap] = []
    identities: set[tuple[str, str | None, str]] = set()
    for gap in gaps:
        identity = (gap.semantic_role.value, gap.capture_id, gap.reason_code)
        if identity not in identities:
            result.append(gap)
            identities.add(identity)
    return tuple(result)


def _record_sealed_evidence_metrics(index: AppEvidenceIndex) -> None:
    """只在首次由 BUILDING 封存后记录不可变 Gap，重放 sealed Index 不重复计数。"""

    visual_reason_codes = {
        "capture_visual_understanding_failed",
        "final_observation_visual_understanding_failed",
    }
    for gap in index.evidence_gaps:
        record_app_metric_safely(
            APP_EXPLORATION_EVIDENCE_GAP_TOTAL,
            semantic_role=gap.semantic_role.value,
            reason_code=gap.reason_code,
        )
        if gap.reason_code in visual_reason_codes:
            error_code = gap.metadata.get("error_code")
            record_app_metric_safely(
                APP_EXPLORATION_VISUAL_FACT_MISSING_TOTAL,
                semantic_role=gap.semantic_role.value,
                reason_code=gap.reason_code,
                error_code=(
                    error_code if isinstance(error_code, str) and error_code.strip() else "unknown"
                ),
            )


def _capture_semantic_role(capture: CaptureEvidenceManifest) -> AppEvidenceSemanticRole:
    explicit = capture.metadata.get("semantic_role")
    if isinstance(explicit, str):
        try:
            return AppEvidenceSemanticRole(explicit)
        except ValueError:
            pass
    has_surface_phase = bool(capture.metadata.get("surface_phase_run_id"))
    mapping = {
        "bootstrap": AppEvidenceSemanticRole.BOOTSTRAP,
        "before_action": (
            AppEvidenceSemanticRole.SURFACE_BEFORE
            if has_surface_phase
            else AppEvidenceSemanticRole.ACTION_BEFORE
        ),
        "after_action": (
            AppEvidenceSemanticRole.SURFACE_AFTER
            if has_surface_phase
            else AppEvidenceSemanticRole.ACTION_AFTER
        ),
        "observation": AppEvidenceSemanticRole.SURFACE_REOBSERVE,
    }
    return mapping.get(capture.capture_role, AppEvidenceSemanticRole.SURFACE_REOBSERVE)
