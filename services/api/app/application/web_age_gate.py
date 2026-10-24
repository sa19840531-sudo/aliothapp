"""Web V2 成年门禁的只读判定与密封候选点击状态机。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationError,
)
from services.api.app.application.web_stale_candidate_recovery import (
    validated_web_stale_before_capture,
    web_recovery_evidence_matches,
)
from services.api.app.domain.exploration import WebInteractionCandidate
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
    build_action_execution_id,
)
from services.api.app.domain.protocol import require_aware_datetime
from services.api.app.domain.task import AdultSelfAttestation


class WebAgeGateReviewRequiredError(RuntimeError):
    """门禁结果不满足自动处理条件，必须进入人工复核。"""


class WebHumanVerificationBlockedError(WebAgeGateReviewRequiredError):
    """检测到验证码或人机验证，自动化必须停止。"""


class WebAdultAttestationRequiredError(WebAgeGateReviewRequiredError):
    """任务没有服务端冻结的有效成年声明。"""


@dataclass(frozen=True, kw_only=True)
class WebAgeGateAssessment:
    """LLM 对成年门禁的闭集结构化判定。"""

    gate_present: bool
    confirmation_candidate_id: str | None
    confidence: float
    reason: str
    human_verification_present: bool

    def __post_init__(self) -> None:
        if not isinstance(self.gate_present, bool) or not isinstance(
            self.human_verification_present, bool
        ):
            raise ValueError("门禁判定字段必须为布尔值")
        if isinstance(self.confidence, bool) or not isinstance(
            self.confidence, (int, float)
        ) or not 0 <= float(self.confidence) <= 1:
            raise ValueError("confidence 必须在 0 到 1 之间")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason 不能为空")
        candidate_id = (
            self.confirmation_candidate_id.strip()
            if isinstance(self.confirmation_candidate_id, str)
            else None
        )
        if self.human_verification_present and candidate_id is not None:
            raise ValueError("人机验证存在时不得返回点击候选")
        if not self.gate_present and candidate_id is not None:
            raise ValueError("门禁不存在时不得返回确认候选")
        if self.gate_present and not self.human_verification_present and candidate_id is None:
            raise ValueError("成年门禁存在时必须返回确认候选")
        object.__setattr__(self, "confirmation_candidate_id", candidate_id)
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "reason", self.reason.strip()[:1000])


class WebAgeGateAssessmentPort(Protocol):
    async def assess(
        self,
        *,
        screenshot_artifact_id: str,
        dom_artifact_id: str,
        candidates: tuple[dict[str, object], ...],
    ) -> WebAgeGateAssessment: ...


@dataclass(frozen=True, kw_only=True)
class WebAgeGateEvidence:
    """一次 Web Capture 中供门禁判定使用的证据引用。"""

    task_id: str
    agent_run_id: str
    session_id: str
    capture_id: str
    screenshot_artifact_id: str
    dom_artifact_id: str
    candidates: tuple[WebInteractionCandidate, ...]
    deadline: datetime
    captured_at: datetime | None = None
    url: str | None = None
    supplemental_screenshot_artifact_ids: tuple[str, ...] = ()
    supplemental_evidence_status: str = "not_attempted"
    supplemental_evidence_reason_code: str | None = None

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
            raise ValueError("门禁候选必须绑定当前 Capture")
        require_aware_datetime(self.deadline, "deadline")


class WebAgeGateEvidenceReader(Protocol):
    async def read(self, *, capture_id: str) -> WebAgeGateEvidence: ...


class WebAgeGateCoordinator(Protocol):
    async def coordinate(self, command: CaptureCoordinationCommand) -> object: ...


class WebAgeGatePageTypeRecordingPort(Protocol):
    async def record(
        self, *, evidence: WebAgeGateEvidence, assessment: WebAgeGateAssessment
    ) -> object: ...


@dataclass(frozen=True, kw_only=True)
class WebAgeGateHandlingResult:
    pre_gate_capture_id: str
    gate_present: bool
    dismiss_action_id: str | None
    clicked_candidate_id: str | None
    post_gate_capture_id: str | None
    gate_disappeared: bool

    @property
    def entry_capture_id(self) -> str:
        return self.pre_gate_capture_id

    @property
    def after_capture_id(self) -> str | None:
        return self.post_gate_capture_id


class WebAgeGateHandler:
    """先只读判定，再按密封候选 ID 点击并复验门禁消失。"""

    def __init__(
        self,
        *,
        assessor: WebAgeGateAssessmentPort,
        coordinator: WebAgeGateCoordinator,
        evidence_reader: WebAgeGateEvidenceReader,
        classification_minimum_confidence: float = 0.7,
        action_minimum_confidence: float = 0.8,
        page_type_recorder: WebAgeGatePageTypeRecordingPort | None = None,
    ) -> None:
        if not 0.7 <= classification_minimum_confidence <= action_minimum_confidence <= 1:
            raise ValueError("分类阈值不得低于 0.7，且不得高于动作阈值")
        if action_minimum_confidence < 0.8:
            raise ValueError("动作阈值不得低于 0.8")
        self._assessor = assessor
        self._coordinator = coordinator
        self._evidence_reader = evidence_reader
        self._classification_minimum_confidence = classification_minimum_confidence
        self._action_minimum_confidence = action_minimum_confidence
        self._page_type_recorder = page_type_recorder

    async def handle(
        self,
        evidence: WebAgeGateEvidence,
        *,
        adult_self_attestation: AdultSelfAttestation | None,
    ) -> WebAgeGateHandlingResult:
        assessment = await self._assess(evidence)
        if assessment.human_verification_present:
            raise WebHumanVerificationBlockedError("检测到人机验证，禁止自动点击")
        if not assessment.gate_present:
            return WebAgeGateHandlingResult(
                pre_gate_capture_id=evidence.capture_id,
                gate_present=False,
                dismiss_action_id=None,
                clicked_candidate_id=None,
                post_gate_capture_id=None,
                gate_disappeared=True,
            )
        if (
            assessment.confidence >= self._classification_minimum_confidence
            and self._page_type_recorder is not None
        ):
            await self._page_type_recorder.record(evidence=evidence, assessment=assessment)
        if not isinstance(adult_self_attestation, AdultSelfAttestation) or (
            adult_self_attestation.declared is not True
        ):
            raise WebAdultAttestationRequiredError("Web 任务缺少有效的操作者成年声明")
        if assessment.confidence < self._action_minimum_confidence:
            raise WebAgeGateReviewRequiredError("成年门禁判定置信度不足")
        candidate_id = assessment.confirmation_candidate_id
        candidate = next(
            (item for item in evidence.candidates if item.candidate_id == candidate_id),
            None,
        )
        if candidate is None or candidate.capability != "web_age_gate_confirm":
            raise WebAgeGateReviewRequiredError("成年确认候选不属于当前安全候选闭集")

        intent = _age_gate_intent(evidence=evidence, candidate=candidate)
        try:
            result = await self._coordinator.coordinate(
                CaptureCoordinationCommand(
                    agent_run_id=evidence.agent_run_id,
                    intent=intent,
                )
            )
        except CaptureCoordinationError as error:
            before_capture = validated_web_stale_before_capture(
                error,
                intent=intent,
            )
            if before_capture is None:
                raise
            fresh_capture_id = before_capture.capture_id
            fresh_evidence = await self._evidence_reader.read(
                capture_id=fresh_capture_id
            )
            if not web_recovery_evidence_matches(
                fresh_evidence,
                original=evidence,
                expected_capture_id=fresh_capture_id,
            ):
                raise
            fresh_assessment = await self._assess(fresh_evidence)
            if fresh_assessment.human_verification_present:
                raise WebHumanVerificationBlockedError(
                    "动作前复检发现人机验证，禁止继续"
                ) from error
            if fresh_assessment.gate_present:
                raise WebAgeGateReviewRequiredError(
                    "成年确认候选漂移且门禁仍然存在"
                ) from error
            return WebAgeGateHandlingResult(
                pre_gate_capture_id=evidence.capture_id,
                gate_present=True,
                dismiss_action_id=None,
                clicked_candidate_id=None,
                post_gate_capture_id=fresh_capture_id,
                gate_disappeared=True,
            )
        after_capture = getattr(result, "after_capture", None)
        after_capture_id = str(getattr(after_capture, "capture_id", "") or "")
        if not after_capture_id or after_capture_id == evidence.capture_id:
            raise WebAgeGateReviewRequiredError("成年门禁点击后缺少 Capture")
        after_evidence = await self._evidence_reader.read(capture_id=after_capture_id)
        if not web_recovery_evidence_matches(
            after_evidence, original=evidence, expected_capture_id=after_capture_id
        ):
            raise WebAgeGateReviewRequiredError("成年门禁点击后 Capture 归属不一致")
        after_assessment = await self._assess(after_evidence)
        if after_assessment.human_verification_present:
            raise WebHumanVerificationBlockedError("点击后出现人机验证，禁止继续")
        if after_assessment.gate_present:
            raise WebAgeGateReviewRequiredError("成年门禁点击后未消失")
        return WebAgeGateHandlingResult(
            pre_gate_capture_id=evidence.capture_id,
            gate_present=True,
            dismiss_action_id=build_action_execution_id(intent),
            clicked_candidate_id=candidate.candidate_id,
            post_gate_capture_id=after_capture_id,
            gate_disappeared=True,
        )

    async def _assess(self, evidence: WebAgeGateEvidence) -> WebAgeGateAssessment:
        return await self._assessor.assess(
            screenshot_artifact_id=evidence.screenshot_artifact_id,
            dom_artifact_id=evidence.dom_artifact_id,
            candidates=tuple(item.to_public_dict() for item in evidence.candidates),
        )


def _age_gate_intent(
    *,
    evidence: WebAgeGateEvidence,
    candidate: WebInteractionCandidate,
) -> ActionIntent:
    profile = CaptureProfile(
        profile_id="web-age-gate-confirm.v1",
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
    )
    intent_id = f"web-age-gate:{evidence.capture_id}"
    parameters: Mapping[str, object] = {
        "target_mode": "web_sealed_candidate",
        "web_sealed_candidate_id": candidate.candidate_id,
        "web_sealed_source_capture_id": evidence.capture_id,
        "web_sealed_candidate_capability": candidate.capability,
        "web_capture_stage": "web_page_probe",
        "clear_obstructions": False,
    }
    return ActionIntent(
        task_id=evidence.task_id,
        session_id=evidence.session_id,
        intent_id=intent_id,
        idempotency_key=intent_id,
        surface_kind=SurfaceKind.WEB,
        source_capture_id=evidence.capture_id,
        action_type=InteractionOperation.CLICK,
        semantic_target="确认已成年并进入",
        purpose="依据操作者成年声明确认年龄门禁并进入站点",
        parameters=parameters,
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=profile,
        deadline=evidence.deadline,
    )
__all__ = [
    "WebAdultAttestationRequiredError",
    "WebAgeGateAssessment",
    "WebAgeGateAssessmentPort",
    "WebAgeGateEvidence",
    "WebAgeGateEvidenceReader",
    "WebAgeGateHandler",
    "WebAgeGateHandlingResult",
    "WebAgeGateReviewRequiredError",
    "WebHumanVerificationBlockedError",
]
