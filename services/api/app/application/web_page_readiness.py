"""Web 页面逐层阻断清理和无遮挡基线状态机。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, TypeVar

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationError,
)
from services.api.app.application.web_age_gate import (
    WebAgeGateEvidence,
    WebAgeGateHandlingResult,
    WebAgeGateReviewRequiredError,
    WebHumanVerificationBlockedError,
)
from services.api.app.application.web_assessment_errors import (
    WebAssessmentTransientServiceError,
)
from services.api.app.application.web_exploration_workflow import (
    WebCaptureStage,
    web_capture_profile,
)
from services.api.app.application.web_interaction_facts import (
    WebInteractionCaptureFacts,
    web_business_identity_stable,
    web_command_accepted,
    web_receipt_allows_arrival,
)
from services.api.app.application.web_interaction_precision import WebInteractionOutcome
from services.api.app.application.web_obstruction_outcome import settle_web_obstruction_dismissal
from services.api.app.application.web_readiness_contracts import (
    WebEvidenceRole,
    WebNetworkCapability,
    WebReadinessSignals,
    WebReadinessStatus,
    WebSettleAssessment,
)
from services.api.app.application.web_stale_candidate_recovery import (
    validated_web_retryable_timeout_before_capture,
    validated_web_stale_before_capture,
    web_recovery_evidence_matches,
)
from services.api.app.domain.exploration import WebInteractionCandidate
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.protocol import require_aware_datetime
from services.api.app.domain.task import AdultSelfAttestation

WEB_READINESS_DIAGNOSTIC_CODES = frozenset({
    "WEB_OBSTRUCTION_CANDIDATES_MISSING",
    "WEB_OBSTRUCTION_CANDIDATES_AMBIGUOUS",
    "WEB_OBSTRUCTION_CANDIDATE_OUTSIDE_CLOSED_SET",
    "WEB_OBSTRUCTION_REVIEW_REQUIRED",
    "WEB_OBSTRUCTION_ASSESSMENT_INVALID",
    "WEB_OBSTRUCTION_ASSESSMENT_UNAVAILABLE",
})


class WebObstructionReviewRequiredError(RuntimeError):
    """存在普通阻断，但当前 Capture 无安全关闭候选。"""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "WEB_OBSTRUCTION_CANDIDATES_MISSING",
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, kw_only=True)
class WebObstructionAssessment:
    """LLM 对普通阻断层的闭集只读判定。"""

    obstruction_present: bool
    dismiss_candidate_id: str | None
    confidence: float
    reason: str
    human_verification_present: bool
    age_gate_present: bool

    def __post_init__(self) -> None:
        for field_name in (
            "obstruction_present",
            "human_verification_present",
            "age_gate_present",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} 必须为布尔值")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, int | float)
            or not 0 <= float(self.confidence) <= 1
        ):
            raise ValueError("confidence 必须在 0 到 1 之间")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason 不能为空")
        candidate_id = (
            self.dismiss_candidate_id.strip()
            if isinstance(self.dismiss_candidate_id, str)
            else None
        )
        if self.human_verification_present or self.age_gate_present:
            if candidate_id is not None:
                raise ValueError("特殊门禁存在时不得返回普通关闭候选")
        elif self.obstruction_present and candidate_id is None:
            raise ValueError("普通阻断存在时必须返回关闭候选")
        elif not self.obstruction_present and candidate_id is not None:
            raise ValueError("普通阻断不存在时不得返回关闭候选")
        object.__setattr__(self, "dismiss_candidate_id", candidate_id)
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "reason", self.reason.strip()[:1000])


class WebObstructionAssessmentPort(Protocol):
    async def assess_obstruction(
        self,
        *,
        screenshot_artifact_id: str,
        dom_artifact_id: str,
        candidates: tuple[dict[str, object], ...],
        page_role: str,
        obstruction_geometry: Mapping[str, object] | None = None,
    ) -> WebObstructionAssessment: ...


@dataclass(frozen=True, kw_only=True)
class WebPageEvidence:
    """单次页面探测中供普通阻断判定使用的证据引用。"""

    task_id: str
    agent_run_id: str
    session_id: str
    capture_id: str
    screenshot_artifact_id: str
    dom_artifact_id: str
    candidates: tuple[WebInteractionCandidate, ...]
    deadline: datetime
    obstruction_state_fingerprint: str | None = None
    benign_ordinary_overlays_only: bool = False
    obstruction_geometry: Mapping[str, object] | None = None
    readiness: WebSettleAssessment | None = None
    interaction_facts: WebInteractionCaptureFacts | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "agent_run_id",
            "session_id",
            "capture_id",
            "screenshot_artifact_id",
            "dom_artifact_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 不能为空")
        if any(not isinstance(item, WebInteractionCandidate) for item in self.candidates):
            raise ValueError("candidates 必须为 WebInteractionCandidate")
        if any(item.capture_id != self.capture_id for item in self.candidates):
            raise ValueError("阻断候选必须绑定当前 Capture")
        require_aware_datetime(self.deadline, "deadline")
        fingerprint = (
            self.obstruction_state_fingerprint.strip().casefold()
            if isinstance(self.obstruction_state_fingerprint, str)
            else None
        )
        if fingerprint is not None and (
            len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError("obstruction_state_fingerprint 必须为 64 位十六进制")
        object.__setattr__(self, "obstruction_state_fingerprint", fingerprint)
        if self.readiness is not None and not isinstance(
            self.readiness,
            WebSettleAssessment,
        ):
            raise ValueError("readiness 必须为 WebSettleAssessment 或 None")
        if not isinstance(self.benign_ordinary_overlays_only, bool):
            raise ValueError("benign_ordinary_overlays_only 必须为布尔值")


class WebPageEvidenceReader(Protocol):
    async def read(self, *, capture_id: str) -> WebPageEvidence: ...


class WebPageActionCoordinator(Protocol):
    async def coordinate(self, command: CaptureCoordinationCommand) -> object: ...


class WebPageAgeGateHandler(Protocol):
    async def handle(
        self,
        evidence: WebAgeGateEvidence,
        *,
        adult_self_attestation: AdultSelfAttestation | None,
    ) -> WebAgeGateHandlingResult: ...


@dataclass(frozen=True, kw_only=True)
class WebPageReadinessResult:
    status: WebReadinessStatus
    ready_capture_id: str | None
    dismissed_layers: tuple[str, ...]
    evidence_capture_ids: tuple[str, ...]
    role: WebEvidenceRole = WebEvidenceRole.CLASSIFICATION
    signals: WebReadinessSignals | None = None
    missing_signals: tuple[str, ...] = ()
    identity_before: str | None = None
    identity_after: str | None = None
    network_capability: WebNetworkCapability = WebNetworkCapability.UNKNOWN
    blocking_reasons: tuple[str, ...] = ()
    dynamic_reasons: tuple[str, ...] = ()
    interaction_outcomes: tuple[WebInteractionOutcome, ...] = ()
    interaction_action_refs: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        status = (
            WebReadinessStatus.STABLE if self.status == "ready" else WebReadinessStatus(self.status)
        )
        object.__setattr__(self, "status", status)
        if self.signals is not None:
            object.__setattr__(self, "identity_before", self.signals.identity_before)
            object.__setattr__(self, "identity_after", self.signals.identity_after)
            object.__setattr__(
                self,
                "network_capability",
                self.signals.network_capability,
            )

    @property
    def is_ready(self) -> bool:
        return self.status is WebReadinessStatus.STABLE and self.ready_capture_id is not None


class WebPageReadinessCoordinator:
    """逐层关闭普通阻断；特殊门禁、漂移和预算耗尽均失败关闭。"""

    MAX_LAYERS = 12
    # Web 截图、DOM、模型判断和动作封存均可能是慢步骤；保留 12 层与最终复检余量。
    MAX_ELAPSED_SECONDS = 900.0
    STABLE_WAIT_SECONDS = 5.0

    def __init__(
        self,
        *,
        assessor: WebObstructionAssessmentPort,
        coordinator: WebPageActionCoordinator,
        evidence_reader: WebPageEvidenceReader,
        age_gate_handler: WebPageAgeGateHandler | None = None,
        adult_self_attestation: AdultSelfAttestation | None = None,
        minimum_confidence: float = 0.8,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        failure_sequence_notifier: Callable[..., Awaitable[object]] | None = None,
        interaction_sequence_notifier: Callable[..., Awaitable[object]] | None = None,
        popup_blocked_recorder: Callable[..., Awaitable[object]] | None = None,
    ) -> None:
        if not 0 <= minimum_confidence <= 1:
            raise ValueError("minimum_confidence 必须在 0 到 1 之间")
        self._assessor = assessor
        self._coordinator = coordinator
        self._evidence_reader = evidence_reader
        self._age_gate_handler = age_gate_handler
        self._adult_self_attestation = adult_self_attestation
        self._minimum_confidence = minimum_confidence
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._failure_sequence_notifier = failure_sequence_notifier
        self._interaction_sequence_notifier = interaction_sequence_notifier
        self._popup_blocked_recorder = popup_blocked_recorder

    async def prepare(
        self,
        *,
        source_capture_id: str,
        page_role: str,
        age_gate_verified_absent_capture_id: str | None = None,
    ) -> WebPageReadinessResult:
        outcomes: list[WebInteractionOutcome] = []
        action_refs: list[dict[str, object]] = []
        result = await self._prepare(
            outcomes=outcomes,
            action_refs=action_refs,
            source_capture_id=source_capture_id,
            page_role=page_role,
            age_gate_verified_absent_capture_id=age_gate_verified_absent_capture_id,
        )
        result = replace(
            result,
            interaction_outcomes=tuple(outcomes),
            interaction_action_refs=tuple(action_refs),
        )
        if (
            result.status is WebReadinessStatus.POPUP_BLOCKED
            and result.evidence_capture_ids
            and self._popup_blocked_recorder is not None
            and not {
                "WEB_OBSTRUCTION_ASSESSMENT_INVALID",
                "WEB_OBSTRUCTION_ASSESSMENT_UNAVAILABLE",
            }.intersection(result.blocking_reasons)
        ):
            # 仅标记当前阻断证据；仅探索任务同样落库，不依赖正式质检通知开关。
            await self._popup_blocked_recorder(
                capture_id=result.evidence_capture_ids[-1],
                blocking_reasons=result.blocking_reasons,
            )
        if (
            result.is_ready
            and result.interaction_outcomes
            and self._interaction_sequence_notifier is not None
        ):
            try:
                await self._interaction_sequence_notifier(
                    source_capture_id=source_capture_id,
                    page_role=page_role,
                    result=result,
                )
            except Exception:  # noqa: BLE001 - 序列发布异常不改原始 readiness 或稳定分类流程
                logging.getLogger(__name__).exception("Web readiness 动作序列通知未完成")
        if not result.is_ready and self._failure_sequence_notifier is not None:
            try:
                await self._failure_sequence_notifier(
                    source_capture_id=source_capture_id,
                    page_role=page_role,
                    result=result,
                )
            except Exception:  # noqa: BLE001 - 发布异常不得抹去真实失败及其动作血缘
                logging.getLogger(__name__).exception("Web readiness 失败序列通知未完成")
        return result

    async def refresh_ready_baseline(
        self,
        *,
        source_capture_id: str,
        page_role: str,
    ) -> str:
        """补充截图不改变当前业务页，复核并沿用已签发的稳定基线。"""

        if not isinstance(source_capture_id, str) or not source_capture_id.strip():
            raise ValueError("source_capture_id 不能为空")
        if page_role not in {"home", "menu", "detail"}:
            raise ValueError("page_role 不受支持")
        evidence = await self._evidence_reader.read(capture_id=source_capture_id)
        readiness = evidence.readiness
        if readiness is None or readiness.status is not WebReadinessStatus.STABLE:
            raise RuntimeError("WEB_POST_SUPPLEMENTAL_BASELINE_SOURCE_NOT_READY")
        # 证据读取器已校验来源仍为当前 Capture；重复 observe 可能被动态页面
        # 的背景变更污染，不能用新诊断帧推翻已签发且未导航的稳定基线。
        return evidence.capture_id

    async def _prepare(
        self,
        *,
        action_refs: list[dict[str, object]],
        source_capture_id: str,
        page_role: str,
        age_gate_verified_absent_capture_id: str | None = None,
        outcomes: list[WebInteractionOutcome],
    ) -> WebPageReadinessResult:
        if not isinstance(source_capture_id, str) or not source_capture_id.strip():
            raise ValueError("source_capture_id 不能为空")
        if page_role not in {"home", "menu", "detail"}:
            raise ValueError("page_role 不受支持")
        if age_gate_verified_absent_capture_id is not None and (
            not isinstance(age_gate_verified_absent_capture_id, str)
            or not age_gate_verified_absent_capture_id.strip()
        ):
            raise ValueError("age_gate_verified_absent_capture_id 不能为空")
        started_at = self._monotonic()
        evidence_ids: list[str] = []
        dismissed: list[str] = []
        attempted_dismiss_selectors: dict[str, set[str]] = {}
        pending_close = None
        unresolved_refresh_attempted = False
        age_gate_verified_absent = source_capture_id == age_gate_verified_absent_capture_id

        try:
            current = await self._call_with_budget(
                lambda: self._evidence_reader.read(capture_id=source_capture_id),
                started_at=started_at,
            )
            evidence_ids.append(current.capture_id)
            if (
                current.readiness is not None
                and current.readiness.status is WebReadinessStatus.CAPTURE_FAILED
                and "blank_document" in current.readiness.blocking_reasons
            ):
                return _settle_result(
                    assessment=current.readiness,
                    capture_id=current.capture_id,
                    dismissed_layers=tuple(dismissed),
                    evidence_capture_ids=tuple(evidence_ids),
                )
            # 允许最多关闭 12 层，但关闭第 12 层后仍必须再做一次最终复检。
            for layer in range(self.MAX_LAYERS + 1):
                if _has_age_gate_candidate(current) and self._age_gate_handler is not None:
                    age_result = await self._handle_age_gate(
                        current,
                        dismissed=dismissed,
                        evidence_ids=evidence_ids,
                        started_at=started_at,
                    )
                    if isinstance(age_result, WebPageReadinessResult):
                        return age_result
                    if age_result.after_capture_id:
                        age_gate_verified_absent = age_result.gate_disappeared
                        current = await self._read_age_gate_after_capture(
                            age_result.after_capture_id,
                            evidence_ids=evidence_ids,
                            started_at=started_at,
                        )
                        continue
                    if age_result.gate_present:
                        return _blocked_result("popup_blocked", dismissed, evidence_ids)
                    age_gate_verified_absent = True
                deterministic_clear_after_dismissal = _is_deterministically_clear_after_dismissal(
                    current=current,
                    pending_close=pending_close,
                    outcomes=outcomes,
                )
                obstruction_signature = _obstruction_signature(current)
                attempted_for_current_obstruction = attempted_dismiss_selectors.get(
                    obstruction_signature,
                    set(),
                )
                assessment_evidence = _without_attempted_ordinary_candidates(
                    current,
                    attempted_selectors=attempted_for_current_obstruction,
                )
                if (
                    attempted_for_current_obstruction
                    and any(
                        candidate.capability == "web_obstruction_dismiss"
                        for candidate in current.candidates
                    )
                    and not any(
                        candidate.capability == "web_obstruction_dismiss"
                        for candidate in assessment_evidence.candidates
                    )
                ):
                    return _blocked_result("popup_blocked", dismissed, evidence_ids)
                try:
                    if (current.benign_ordinary_overlays_only
                            and current.obstruction_geometry is None):
                        assessment = WebObstructionAssessment(
                            obstruction_present=False,
                            dismiss_candidate_id=None,
                            confidence=1.0,
                            reason="普通浮层与主体内容重叠不足 30%，不构成阻断",
                            human_verification_present=False,
                            age_gate_present=False,
                        )
                    else:
                        assessment = await self._call_with_budget(
                            lambda assessment_evidence=assessment_evidence: self._assess(
                                assessment_evidence,
                                page_role=page_role,
                            ),
                            started_at=started_at,
                        )
                except WebObstructionReviewRequiredError as error:
                    if (
                        error.reason_code == "WEB_OBSTRUCTION_CANDIDATES_MISSING"
                        and deterministic_clear_after_dismissal
                    ):
                        assessment = WebObstructionAssessment(
                            obstruction_present=False,
                            dismiss_candidate_id=None,
                            confidence=1.0,
                            reason="本轮授权关闭后的原阻断层已消失，当前主体稳定且无阻断候选",
                            human_verification_present=False,
                            age_gate_present=False,
                        )
                    elif (
                        error.reason_code != "WEB_OBSTRUCTION_CANDIDATES_MISSING"
                        or unresolved_refresh_attempted
                    ):
                        return _blocked_result(
                            "popup_blocked", dismissed, evidence_ids,
                            diagnostic_code=(
                                error.reason_code
                                if error.reason_code in WEB_READINESS_DIAGNOSTIC_CODES
                                else "WEB_OBSTRUCTION_REVIEW_REQUIRED"
                            ),
                        )
                    else:
                        unresolved_refresh_attempted = True
                        fresh_capture_id = await self._call_with_budget(
                            lambda current=current: self._capture_probe(
                                current,
                                page_role=page_role,
                            ),
                            started_at=started_at,
                        )
                        evidence_ids.append(fresh_capture_id)
                        await self._call_with_budget(
                            lambda: self._sleeper(self.STABLE_WAIT_SECONDS),
                            started_at=started_at,
                        )
                        current = await self._call_with_budget(
                            lambda fresh_capture_id=fresh_capture_id: self._evidence_reader.read(
                                capture_id=fresh_capture_id
                            ),
                            started_at=started_at,
                        )
                        continue
                except (TypeError, ValueError, WebAssessmentTransientServiceError) as error:
                    # 严格结构化判定漂移或模型服务明确 5xx 时只读补采一次；
                    # 第二份独立证据仍失败则关闭，禁止据此执行任何点击。
                    if unresolved_refresh_attempted:
                        return _blocked_result(
                            "popup_blocked", dismissed, evidence_ids,
                            diagnostic_code=(
                                "WEB_OBSTRUCTION_ASSESSMENT_UNAVAILABLE"
                                if isinstance(error, WebAssessmentTransientServiceError)
                                else "WEB_OBSTRUCTION_ASSESSMENT_INVALID"
                            ),
                        )
                    unresolved_refresh_attempted = True
                    fresh_capture_id = await self._call_with_budget(
                        lambda current=current: self._capture_probe(
                            current,
                            page_role=page_role,
                        ),
                        started_at=started_at,
                    )
                    evidence_ids.append(fresh_capture_id)
                    await self._call_with_budget(
                        lambda: self._sleeper(self.STABLE_WAIT_SECONDS),
                        started_at=started_at,
                    )
                    current = await self._call_with_budget(
                        lambda fresh_capture_id=fresh_capture_id: self._evidence_reader.read(
                            capture_id=fresh_capture_id
                        ),
                        started_at=started_at,
                    )
                    continue
                if (
                    assessment.age_gate_present
                    and _adult_visual_age_false_positive_can_be_rechecked(
                        current,
                        adult_self_attestation=self._adult_self_attestation,
                    )
                ):
                    if not unresolved_refresh_attempted:
                        unresolved_refresh_attempted = True
                        fresh_capture_id = await self._call_with_budget(
                            lambda current=current: self._capture_probe(
                                current,
                                page_role=page_role,
                            ),
                            started_at=started_at,
                        )
                        evidence_ids.append(fresh_capture_id)
                        await self._call_with_budget(
                            lambda: self._sleeper(self.STABLE_WAIT_SECONDS),
                            started_at=started_at,
                        )
                        current = await self._call_with_budget(
                            lambda fresh_capture_id=fresh_capture_id: self._evidence_reader.read(
                                capture_id=fresh_capture_id
                            ),
                            started_at=started_at,
                        )
                        continue
                    assessment = replace(
                        assessment,
                        obstruction_present=False,
                        age_gate_present=False,
                        reason="成年声明存在；两份独立 Capture 均证明业务层可见且无结构化年龄门禁",
                    )
                if assessment.human_verification_present:
                    return _blocked_result("human_verification_blocked", dismissed, evidence_ids)
                if assessment.age_gate_present:
                    if self._age_gate_handler is None:
                        return _blocked_result("popup_blocked", dismissed, evidence_ids)
                    if not age_gate_verified_absent:
                        age_result = await self._handle_age_gate(
                            current,
                            dismissed=dismissed,
                            evidence_ids=evidence_ids,
                            started_at=started_at,
                        )
                        if isinstance(age_result, WebPageReadinessResult):
                            return age_result
                        if age_result.after_capture_id:
                            age_gate_verified_absent = age_result.gate_disappeared
                            current = await self._read_age_gate_after_capture(
                                age_result.after_capture_id,
                                evidence_ids=evidence_ids,
                                started_at=started_at,
                            )
                            continue
                        if age_result.gate_present:
                            return _blocked_result("popup_blocked", dismissed, evidence_ids)
                    candidate = _unique_ordinary_dismiss_candidate(current)
                    if candidate is None:
                        return _blocked_result("popup_blocked", dismissed, evidence_ids)
                elif not assessment.obstruction_present or deterministic_clear_after_dismissal:
                    ready_capture_id = await self._call_with_budget(
                        lambda current=current: self._capture_ready_baseline(
                            current,
                            page_role=page_role,
                        ),
                        started_at=started_at,
                    )
                    evidence_ids.append(ready_capture_id)
                    ready_evidence = await self._call_with_budget(
                        lambda ready_capture_id=ready_capture_id: self._evidence_reader.read(
                            capture_id=ready_capture_id
                        ),
                        started_at=started_at,
                    )
                    readiness = ready_evidence.readiness
                    if pending_close is not None and outcomes[-1].postcondition_satisfied:
                        close_intent, close_selector, close_before, close_authorized = pending_close
                        outcomes[-1] = settle_web_obstruction_dismissal(
                            intent=close_intent,
                            selector=close_selector,
                            before=close_before.interaction_facts,
                            after=(
                                replace(
                                    ready_evidence.interaction_facts,
                                    identity_verified=ready_evidence.interaction_facts.identity_verified
                                    and close_authorized,
                                )
                                if ready_evidence.interaction_facts
                                else None
                            ),
                            after_capture_id=ready_capture_id,
                            command_accepted=outcomes[-1].command_accepted,
                        )
                    if readiness is None:
                        readiness = WebSettleAssessment(
                            role=WebEvidenceRole.CLASSIFICATION,
                            status=WebReadinessStatus.CAPTURE_FAILED,
                            blocking_reasons=("readiness_metadata_missing",),
                        )
                    return _settle_result(
                        assessment=readiness,
                        capture_id=ready_capture_id,
                        dismissed_layers=tuple(dismissed),
                        evidence_capture_ids=tuple(evidence_ids),
                    )
                else:
                    if self._popup_blocked_recorder is not None:
                        # 关闭成功也保留原始弹窗页型，清障后的新帧再走正常页面识别。
                        await self._popup_blocked_recorder(
                            capture_id=current.capture_id,
                            blocking_reasons=("popup_blocked",),
                        )
                    candidate = _selected_dismiss_candidate(current, assessment)
                if layer == self.MAX_LAYERS:
                    break
                unique_ordinary_candidate = _unique_ordinary_dismiss_candidate(current)
                structurally_authorized_dismissal = (
                    candidate is not None
                    and candidate == unique_ordinary_candidate
                    and not assessment.human_verification_present
                    and (
                        age_gate_verified_absent
                        or (
                            assessment.obstruction_present
                            and not assessment.age_gate_present
                        )
                    )
                )
                if (
                    assessment.confidence < self._minimum_confidence
                    and not structurally_authorized_dismissal
                ):
                    break
                if candidate is None:
                    break
                signature = obstruction_signature
                if candidate.selector in attempted_dismiss_selectors.get(signature, set()):
                    break
                attempted_dismiss_selectors.setdefault(signature, set()).add(candidate.selector)
                dismiss_intent = _readiness_intent(
                    evidence=current,
                    action_type=InteractionOperation.CLICK,
                    semantic_target="关闭当前阻断弹窗",
                    purpose=f"关闭 {page_role} 页当前最上层普通阻断",
                    stage=WebCaptureStage.PAGE_PROBE,
                    candidate=candidate,
                )
                close_receipts: list[object] = []
                try:
                    after_capture_id = await self._call_with_budget(
                        lambda current=current, intent=dismiss_intent, receipts=close_receipts: (
                            self._coordinate_and_require_after(intent, current, receipts=receipts)
                        ),
                        started_at=started_at,
                    )
                except CaptureCoordinationError as error:
                    before_capture = validated_web_stale_before_capture(
                        error,
                        intent=dismiss_intent,
                    )
                    if before_capture is None:
                        timeout_before_capture = validated_web_retryable_timeout_before_capture(
                            error,
                            intent=dismiss_intent,
                        )
                        if timeout_before_capture is None:
                            raise
                        timeout_capture_id = timeout_before_capture.capture_id
                        evidence_ids.append(timeout_capture_id)
                        timeout_source = current
                        current = await self._call_with_budget(
                            lambda timeout_capture_id=timeout_capture_id: (
                                self._evidence_reader.read(capture_id=timeout_capture_id)
                            ),
                            started_at=started_at,
                        )
                        if not web_recovery_evidence_matches(
                            current,
                            original=timeout_source,
                            expected_capture_id=timeout_capture_id,
                        ):
                            raise
                        probe_capture_id = await self._call_with_budget(
                            lambda current=current: self._capture_probe(
                                current,
                                page_role=page_role,
                            ),
                            started_at=started_at,
                        )
                        evidence_ids.append(probe_capture_id)
                        await self._call_with_budget(
                            lambda: self._sleeper(self.STABLE_WAIT_SECONDS),
                            started_at=started_at,
                        )
                        current = await self._call_with_budget(
                            lambda probe_capture_id=probe_capture_id: self._evidence_reader.read(
                                capture_id=probe_capture_id
                            ),
                            started_at=started_at,
                        )
                        continue
                    fresh_capture_id = before_capture.capture_id
                    recovery_source = current
                    evidence_ids.append(fresh_capture_id)
                    await self._call_with_budget(
                        lambda: self._sleeper(self.STABLE_WAIT_SECONDS),
                        started_at=started_at,
                    )
                    current = await self._call_with_budget(
                        lambda fresh_capture_id=fresh_capture_id: self._evidence_reader.read(
                            capture_id=fresh_capture_id
                        ),
                        started_at=started_at,
                    )
                    if not web_recovery_evidence_matches(
                        current,
                        original=recovery_source,
                        expected_capture_id=fresh_capture_id,
                    ):
                        raise
                    continue
                close_before = current
                evidence_ids.append(after_capture_id)
                await self._call_with_budget(
                    lambda: self._sleeper(self.STABLE_WAIT_SECONDS),
                    started_at=started_at,
                )
                current = await self._call_with_budget(
                    lambda after_capture_id=after_capture_id: self._evidence_reader.read(
                        capture_id=after_capture_id
                    ),
                    started_at=started_at,
                )
                close_outcome = settle_web_obstruction_dismissal(
                    intent=dismiss_intent,
                    selector=candidate.selector,
                    before=close_before.interaction_facts,
                    after=(
                        replace(
                            current.interaction_facts,
                            identity_verified=current.interaction_facts.identity_verified
                            and web_receipt_allows_arrival(close_receipts[-1]),
                        )
                        if current.interaction_facts
                        else None
                    ),
                    after_capture_id=after_capture_id,
                    command_accepted=web_command_accepted(close_receipts[-1]),
                )
                outcomes.append(close_outcome)
                action_refs.append(
                    {
                        "outcome_ordinal": len(outcomes) - 1,
                        "intent_id": dismiss_intent.intent_id,
                        "requested_source_capture_id": dismiss_intent.source_capture_id,
                        "action_result_capture_id": after_capture_id,
                    }
                )
                pending_close = (
                    dismiss_intent,
                    candidate.selector,
                    close_before,
                    web_receipt_allows_arrival(close_receipts[-1]),
                )
                before_facts, after_facts = (
                    close_before.interaction_facts,
                    current.interaction_facts,
                )
                closed_layer = (
                    dict(before_facts.candidate_layers).get(candidate.selector)
                    if before_facts
                    else None
                )
                if closed_layer and after_facts and closed_layer not in after_facts.visible_layers:
                    dismissed.append(candidate.candidate_id)
        except TimeoutError:
            return _blocked_result(
                WebReadinessStatus.TIMEOUT,
                dismissed,
                evidence_ids,
            )

        return _blocked_result("popup_blocked", dismissed, evidence_ids)

    async def _handle_age_gate(
        self,
        evidence: WebPageEvidence,
        *,
        dismissed: list[str],
        evidence_ids: list[str],
        started_at: float,
    ) -> WebAgeGateHandlingResult | WebPageReadinessResult:
        assert self._age_gate_handler is not None
        try:
            return await self._call_with_budget(
                lambda: self._age_gate_handler.handle(
                    _as_age_gate_evidence(evidence),
                    adult_self_attestation=self._adult_self_attestation,
                ),
                started_at=started_at,
            )
        except WebHumanVerificationBlockedError:
            return _blocked_result("human_verification_blocked", dismissed, evidence_ids)
        except WebAgeGateReviewRequiredError:
            return _blocked_result("popup_blocked", dismissed, evidence_ids)

    async def _read_age_gate_after_capture(
        self,
        capture_id: str,
        *,
        evidence_ids: list[str],
        started_at: float,
    ) -> WebPageEvidence:
        evidence_ids.append(capture_id)
        await self._call_with_budget(
            lambda: self._sleeper(self.STABLE_WAIT_SECONDS),
            started_at=started_at,
        )
        return await self._call_with_budget(
            lambda: self._evidence_reader.read(capture_id=capture_id),
            started_at=started_at,
        )

    async def _call_with_budget(
        self,
        factory: Callable[[], Awaitable[_T]],
        *,
        started_at: float,
    ) -> _T:
        remaining = self.MAX_ELAPSED_SECONDS - (self._monotonic() - started_at)
        if remaining <= 0:
            raise TimeoutError("Web 页面就绪超过 900 秒硬预算")
        return await asyncio.wait_for(factory(), timeout=remaining)

    async def _assess(
        self,
        evidence: WebPageEvidence,
        *,
        page_role: str,
    ) -> WebObstructionAssessment:
        return await self._assessor.assess_obstruction(
            screenshot_artifact_id=evidence.screenshot_artifact_id,
            dom_artifact_id=evidence.dom_artifact_id,
            candidates=tuple(item.to_public_dict() for item in evidence.candidates),
            page_role=page_role,
            **({"obstruction_geometry": evidence.obstruction_geometry}
               if evidence.obstruction_geometry is not None else {}),
        )

    async def _capture_ready_baseline(
        self,
        evidence: WebPageEvidence,
        *,
        page_role: str,
    ) -> str:
        intent = _readiness_intent(
            evidence=evidence,
            action_type=InteractionOperation.OBSERVE,
            semantic_target="采集无遮挡页面基线",
            purpose=f"冻结 {page_role} 页无遮挡基线",
            stage=WebCaptureStage.PAGE_READY_BASELINE,
            candidate=None,
        )
        return await self._coordinate_and_require_after(intent, evidence)

    async def _capture_probe(
        self,
        evidence: WebPageEvidence,
        *,
        page_role: str,
    ) -> str:
        """普通阻断缺少安全候选时，只读补采一次以消除跨证据时序漂移。"""

        intent = _readiness_intent(
            evidence=evidence,
            action_type=InteractionOperation.OBSERVE,
            semantic_target="重新采集当前阻断状态",
            purpose=f"复核 {page_role} 页普通阻断与可信关闭候选",
            stage=WebCaptureStage.PAGE_PROBE,
            candidate=None,
        )
        return await self._coordinate_and_require_after(intent, evidence)

    async def _coordinate_and_require_after(
        self,
        intent: ActionIntent,
        evidence: WebPageEvidence,
        *,
        receipts: list[object] | None = None,
    ) -> str:
        result = await self._coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id=evidence.agent_run_id,
                intent=intent,
            )
        )
        after_capture = getattr(result, "after_capture", None)
        capture_id = str(getattr(after_capture, "capture_id", "") or "")
        if not capture_id:
            raise RuntimeError("页面就绪动作缺少 after Capture")
        if receipts is not None:
            receipts.append(getattr(result, "interaction", None))
        return capture_id


def _selected_dismiss_candidate(
    evidence: WebPageEvidence,
    assessment: WebObstructionAssessment,
) -> WebInteractionCandidate | None:
    return next(
        (
            candidate
            for candidate in evidence.candidates
            if candidate.candidate_id == assessment.dismiss_candidate_id
            and candidate.capability == "web_obstruction_dismiss"
        ),
        None,
    )


def _without_attempted_ordinary_candidates(
    evidence: WebPageEvidence,
    *,
    attempted_selectors: set[str],
) -> WebPageEvidence:
    """同一阻断状态重试时，只向模型暴露尚未尝试的安全关闭候选。"""

    if not attempted_selectors:
        return evidence
    return replace(
        evidence,
        candidates=tuple(
            candidate
            for candidate in evidence.candidates
            if candidate.capability != "web_obstruction_dismiss"
            or candidate.selector not in attempted_selectors
        ),
    )


def _is_deterministically_clear_after_dismissal(
    *,
    current: WebPageEvidence,
    pending_close: tuple[object, str, WebPageEvidence, bool] | None,
    outcomes: list[WebInteractionOutcome],
) -> bool:
    """仅接受本轮已授权关闭后、同一页面主体上的精确阻断层消失事实。"""

    if pending_close is None or not outcomes:
        return False
    _intent, selector, before, close_authorized = pending_close
    outcome = outcomes[-1]
    before_facts = before.interaction_facts
    after_facts = current.interaction_facts
    if before_facts is None or after_facts is None:
        return False
    closed_layer = dict(before_facts.candidate_layers).get(selector)
    return bool(
        close_authorized
        and outcome.command_accepted
        and outcome.effect_observed
        and outcome.postcondition_satisfied
        and outcome.after_capture_id == current.capture_id
        and current.task_id == before.task_id
        and current.agent_run_id == before.agent_run_id
        and current.session_id == before.session_id
        and web_business_identity_stable(before_facts, after_facts)
        and closed_layer
        and closed_layer not in after_facts.visible_layers
        and not current.candidates
        and after_facts.business_layer_visible
        and not after_facts.visible_layers
    )


def _obstruction_signature(
    evidence: WebPageEvidence,
) -> str:
    facts = [
        {
            "candidate_id": item.candidate_id,
            "capability": item.capability,
            "text": item.text,
            "tag_name": item.tag_name,
            "role": item.role,
            "rect": item.rect,
            "effective_z_index": item.effective_z_index,
        }
        for item in evidence.candidates
    ]
    return hashlib.sha256(
        json.dumps(
            {
                "candidates": facts,
                "obstruction_state_fingerprint": (evidence.obstruction_state_fingerprint),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _readiness_intent(
    *,
    evidence: WebPageEvidence,
    action_type: InteractionOperation,
    semantic_target: str,
    purpose: str,
    stage: WebCaptureStage,
    candidate: WebInteractionCandidate | None,
) -> ActionIntent:
    suffix = (
        candidate.candidate_id
        if candidate is not None
        else (stage.value if stage == WebCaptureStage.PAGE_PROBE else "ready")
    )
    intent_identity = f"{evidence.capture_id}:{action_type.value}:{suffix}"
    intent_digest = hashlib.sha256(intent_identity.encode("utf-8")).hexdigest()
    intent_id = f"web-readiness:{action_type.value}:{intent_digest}"
    parameters: dict[str, object] = {
        "web_capture_stage": stage.value,
        "clear_obstructions": False,
    }
    if candidate is not None:
        parameters.update(
            {
                "target_mode": "web_sealed_candidate",
                "web_sealed_candidate_id": candidate.candidate_id,
                "web_sealed_source_capture_id": evidence.capture_id,
                "web_sealed_candidate_capability": candidate.capability,
            }
        )
    return ActionIntent(
        task_id=evidence.task_id,
        session_id=evidence.session_id,
        intent_id=intent_id,
        idempotency_key=intent_id,
        surface_kind=SurfaceKind.WEB,
        source_capture_id=evidence.capture_id,
        action_type=action_type,
        semantic_target=semantic_target,
        purpose=purpose,
        parameters=parameters,
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=web_capture_profile("web-page-readiness.v1"),
        deadline=evidence.deadline,
    )


def _unique_ordinary_dismiss_candidate(
    evidence: WebPageEvidence,
) -> WebInteractionCandidate | None:
    candidates = tuple(
        candidate
        for candidate in evidence.candidates
        if candidate.capability == "web_obstruction_dismiss"
        and candidate.visible
        and candidate.hit_test_passed
    )
    return candidates[0] if len(candidates) == 1 else None


def _has_age_gate_candidate(evidence: WebPageEvidence) -> bool:
    return any(
        candidate.capability == "web_age_gate_confirm"
        and candidate.visible
        and candidate.hit_test_passed
        for candidate in evidence.candidates
    )


def _adult_visual_age_false_positive_can_be_rechecked(
    evidence: WebPageEvidence,
    *,
    adult_self_attestation: AdultSelfAttestation | None,
) -> bool:
    """只允许成年声明下对无结构门禁的清晰业务页做一次独立只读复检。"""

    facts = evidence.interaction_facts
    return bool(
        adult_self_attestation is not None
        and adult_self_attestation.declared is True
        and not _has_age_gate_candidate(evidence)
        and not evidence.candidates
        and facts is not None
        and facts.identity_verified
        and facts.business_layer_visible
        and not facts.visible_layers
    )


def _as_age_gate_evidence(evidence: WebPageEvidence) -> WebAgeGateEvidence:
    return WebAgeGateEvidence(
        task_id=evidence.task_id,
        agent_run_id=evidence.agent_run_id,
        session_id=evidence.session_id,
        capture_id=evidence.capture_id,
        screenshot_artifact_id=evidence.screenshot_artifact_id,
        dom_artifact_id=evidence.dom_artifact_id,
        candidates=evidence.candidates,
        deadline=evidence.deadline,
    )


def _blocked_result(
    status: WebReadinessStatus | str,
    dismissed: list[str],
    evidence_ids: list[str],
    *,
    diagnostic_code: str | None = None,
) -> WebPageReadinessResult:
    resolved_status = WebReadinessStatus(status)
    reason_by_status = {
        WebReadinessStatus.TIMEOUT: "readiness_deadline_exceeded",
        WebReadinessStatus.POPUP_BLOCKED: "popup_blocked",
        WebReadinessStatus.HUMAN_VERIFICATION_BLOCKED: ("human_verification_blocked"),
    }
    return WebPageReadinessResult(
        status=resolved_status,
        ready_capture_id=None,
        dismissed_layers=tuple(dismissed),
        evidence_capture_ids=tuple(evidence_ids),
        # 仅附加固定原因码，禁止把模型响应原文或异常消息写入运行审计。
        blocking_reasons=(
            reason_by_status.get(resolved_status, resolved_status.value),
            *((diagnostic_code,) if diagnostic_code in WEB_READINESS_DIAGNOSTIC_CODES else ()),
        ),
    )


def _settle_result(
    *,
    assessment: WebSettleAssessment,
    capture_id: str,
    dismissed_layers: tuple[str, ...],
    evidence_capture_ids: tuple[str, ...],
) -> WebPageReadinessResult:
    if (
        assessment.status is WebReadinessStatus.STABLE
        and assessment.role is not WebEvidenceRole.CLASSIFICATION
    ):
        assessment = WebSettleAssessment(
            role=assessment.role,
            status=WebReadinessStatus.CAPTURE_FAILED,
            blocking_reasons=("evidence_role_mismatch",),
            dynamic_reasons=assessment.dynamic_reasons,
            signals=assessment.signals,
        )
    signals = assessment.signals
    missing_signals = tuple(
        reason
        for reason in assessment.blocking_reasons
        if reason.endswith("_unknown") or reason.endswith("_missing")
    )
    return WebPageReadinessResult(
        status=assessment.status,
        ready_capture_id=(
            capture_id
            if assessment.status is WebReadinessStatus.STABLE
            and assessment.role is WebEvidenceRole.CLASSIFICATION
            else None
        ),
        dismissed_layers=dismissed_layers,
        evidence_capture_ids=evidence_capture_ids,
        role=assessment.role,
        signals=signals,
        missing_signals=missing_signals,
        blocking_reasons=assessment.blocking_reasons,
        dynamic_reasons=assessment.dynamic_reasons,
    )


def web_readiness_result_is_ready(result: object) -> bool:
    """兼容旧测试桩的 ``ready``，正式结果统一使用 ``stable``。"""

    return getattr(result, "status", None) in {WebReadinessStatus.STABLE, "ready"} and bool(
        getattr(result, "ready_capture_id", None)
    )


__all__ = [
    "WebObstructionAssessment",
    "WebObstructionReviewRequiredError",
    "WebObstructionAssessmentPort",
    "WebPageEvidence",
    "WebPageEvidenceReader",
    "WebPageReadinessCoordinator",
    "WebPageReadinessResult",
    "web_readiness_result_is_ready",
]


_T = TypeVar("_T")
