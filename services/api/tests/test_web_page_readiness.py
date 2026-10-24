"""Web 页面逐层阻断清理与无遮挡基线合同。"""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.agents.explorer.web_assessment_agent import (
    WebExplorationAssessmentAgent,
)
from services.api.app.application.capture_coordination import CaptureCoordinationError
from services.api.app.application.web_age_gate import WebAgeGateReviewRequiredError
from services.api.app.application.web_assessment_errors import (
    WebAssessmentTransientServiceError,
)
from services.api.app.application.web_assessment_evidence import WebAssessmentEvidence
from services.api.app.application.web_interaction_facts import web_capture_content_facts
from services.api.app.application.web_interaction_precision import WebInteractionOutcome
from services.api.app.application.web_page_readiness import (
    WebObstructionAssessment,
    WebObstructionReviewRequiredError,
    WebPageEvidence,
    WebPageReadinessCoordinator,
    _is_deterministically_clear_after_dismissal,
)
from services.api.app.application.web_readiness_contracts import (
    WebEvidenceRole,
    WebNetworkCapability,
    WebReadinessSignals,
    WebReadinessStatus,
    WebSettleAssessment,
    assess_web_readiness,
)
from services.api.app.domain.exploration import (
    build_web_obstruction_candidates,
    web_obstruction_state_fingerprint,
)
from services.api.app.domain.task import AdultSelfAttestation
from services.api.tests.support.fake_chat_client import (
    ScriptedChatClient,
    make_text_response,
)
from services.api.tests.web_stale_candidate_test_support import (
    retryable_web_action_timeout_error,
    stale_web_candidate_error,
)

_READINESS_UNSET = object()


def _evidence(
    capture_id: str,
    *,
    text: str | None,
    selector: str = "#close",
    obstruction_state_fingerprint: str | None = None,
    readiness=_READINESS_UNSET,
) -> WebPageEvidence:
    snapshot = {
        "elements": []
        if text is None
        else [
            {
                "selector": selector,
                "tag": "button",
                "role": "button",
                "text": text,
                "class_tokens": ["modal-close"],
                "ancestor_class_tokens": ["modal"],
                "web_obstruction_layer": "#page-modal",
                "web_obstruction_layer_kind": "modal",
                "web_obstruction_layer_structural": True,
                "web_obstruction_close_semantics": True,
                "within_web_navigation": False,
                "rect": {"x": 1800, "y": 30, "width": 50, "height": 50},
                "interactive": True,
                "visible": True,
                "effective_z_index": 900,
                "hit_test": {"center_hits_target": True},
            }
        ]
    }
    if readiness is _READINESS_UNSET:
        readiness = _settle_assessment() if capture_id.endswith("-ready") else None
    snapshot["elements"].append(
        {"tag": "main", "selector": "#business", "visible": True, "text": "原业务正文"}
    )
    return WebPageEvidence(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id=capture_id,
        screenshot_artifact_id=f"{capture_id}:screenshot",
        dom_artifact_id=f"{capture_id}:dom",
        candidates=build_web_obstruction_candidates(
            capture_id=capture_id,
            ui_snapshot=snapshot,
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        obstruction_state_fingerprint=obstruction_state_fingerprint,
        readiness=readiness,
        interaction_facts=replace(
            web_capture_content_facts(snapshot, url="https://example.test/detail/1"),
            identity_verified=True,
            readiness_accepted=bool(readiness and readiness.can_serve_evidence),
        ),
    )


def _settle_assessment(
    *,
    status: WebReadinessStatus = WebReadinessStatus.STABLE,
    role: WebEvidenceRole = WebEvidenceRole.CLASSIFICATION,
):
    overrides = {
        WebReadinessStatus.LOADING: {"visible_loader": True},
        WebReadinessStatus.TRANSITIONAL: {"transition_active": True},
        WebReadinessStatus.IDENTITY_DRIFT: {"identity_after": "https://example.test/detail/2"},
    }.get(status, {})
    values = {
        "identity_before": "https://example.test/detail/1",
        "identity_after": "https://example.test/detail/1",
        "artifacts_same_epoch": True,
        "capture_failed": False,
        "timed_out": status is WebReadinessStatus.TIMEOUT,
        "core_present": True,
        "visible_loader": False,
        "transition_active": False,
        "necessary_media_ready": True,
        "dom_quiet_ms": 1_200.0,
        "minimum_dom_quiet_ms": 600.0,
        "network_capability": WebNetworkCapability.UNKNOWN,
        "network_required": False,
        "critical_network_pending": None,
        "settle_complete": True,
        "quorum_met": True,
        "capture_consistent": True,
        "profile_version": "web-structure.v1",
        **overrides,
    }
    return assess_web_readiness(
        WebReadinessSignals(**values),
        role=role,
    )


class EvidenceReader:
    def __init__(self, evidence_by_id):
        self.evidence_by_id = evidence_by_id
        self.calls = []

    async def read(self, *, capture_id):
        self.calls.append(capture_id)
        return self.evidence_by_id[capture_id]


class ScriptedAssessor:
    def __init__(self, evidence_by_artifact):
        self.evidence_by_artifact = evidence_by_artifact

    async def assess_obstruction(self, **kwargs):
        evidence = self.evidence_by_artifact[kwargs["screenshot_artifact_id"]]
        assert all("selector" not in item for item in kwargs["candidates"])
        if evidence.candidates:
            return WebObstructionAssessment(
                obstruction_present=True,
                dismiss_candidate_id=evidence.candidates[0].candidate_id,
                confidence=0.98,
                reason="高层弹窗遮挡主体内容",
                human_verification_present=False,
                age_gate_present=False,
            )
        return WebObstructionAssessment(
            obstruction_present=False,
            dismiss_candidate_id=None,
            confidence=0.99,
            reason="页面无遮挡",
            human_verification_present=False,
            age_gate_present=False,
        )


@pytest.mark.asyncio
async def test_blank_entry_capture_fails_before_model_obstruction_assessment() -> None:
    """已由采集层证明为空白的入口不得继续消耗模型和动作预算。"""

    class AssessorMustNotRun:
        async def assess_obstruction(self, **kwargs):
            raise AssertionError("空白文档不得进入模型阻断判定")

    async def no_sleep(_seconds: float) -> None:
        return None

    blank = _evidence(
        "entry-blank",
        text=None,
        readiness=WebSettleAssessment(
            role=WebEvidenceRole.ENTRY_OBSERVATION,
            status=WebReadinessStatus.CAPTURE_FAILED,
            blocking_reasons=("blank_document",),
        ),
    )
    coordinator = WebPageReadinessCoordinator(
        assessor=AssessorMustNotRun(),
        coordinator=Coordinator([]),
        evidence_reader=EvidenceReader({blank.capture_id: blank}),
        sleeper=no_sleep,
    )

    result = await coordinator.prepare(source_capture_id=blank.capture_id, page_role="home")

    assert result.status is WebReadinessStatus.CAPTURE_FAILED
    assert result.blocking_reasons == ("blank_document",)
    assert result.evidence_capture_ids == (blank.capture_id,)


class Coordinator:
    def __init__(self, capture_ids):
        self.capture_ids = iter(capture_ids)
        self.intents = []
        self.commands = []

    async def coordinate(self, command):
        self.commands.append(command)
        self.intents.append(command.intent)
        return SimpleNamespace(
            after_capture=SimpleNamespace(capture_id=next(self.capture_ids)),
            interaction=SimpleNamespace(execution_outcome="succeeded", status="succeeded"),
        )


class ScriptedAgeGateHandler:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    async def handle(self, evidence, *, adult_self_attestation):
        self.calls.append((evidence, adult_self_attestation))
        return next(self.results)


@pytest.mark.asyncio
async def test_refresh_ready_baseline_reuses_current_stable_capture_after_supplemental() -> None:
    source = _evidence("capture-ready", text=None)
    coordinator = Coordinator([])

    class AssessorMustNotRun:
        async def assess_obstruction(self, **_kwargs):
            raise AssertionError("补图后的权威基线刷新不得重复模型阻断判定")

    subject = WebPageReadinessCoordinator(
        assessor=AssessorMustNotRun(),
        coordinator=coordinator,
        evidence_reader=EvidenceReader({source.capture_id: source}),
    )

    capture_id = await subject.refresh_ready_baseline(
        source_capture_id=source.capture_id,
        page_role="home",
    )

    assert capture_id == source.capture_id
    assert coordinator.intents == []


def _adult_self_attestation() -> AdultSelfAttestation:
    return AdultSelfAttestation(
        declared=True,
        statement_version="adult-v1",
        declared_by="operator-1",
        declared_at=datetime(2026, 9, 4, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_closes_multiple_obstructions_until_ready() -> None:
    evidence = [
        _evidence("capture-1", text="关闭广告"),
        _evidence("capture-2", text="稍后再说", selector="#later"),
        _evidence("capture-3", text="我知道了", selector="#known"),
        _evidence("capture-clear", text=None),
        _evidence("capture-ready", text=None),
    ]
    by_id = {item.capture_id: item for item in evidence}
    by_artifact = {item.screenshot_artifact_id: item for item in evidence}
    coordinator = Coordinator(["capture-2", "capture-3", "capture-clear", "capture-ready"])

    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor(by_artifact),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(by_id),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id="capture-1", page_role="menu")

    assert result.status is WebReadinessStatus.STABLE
    assert len(result.dismissed_layers) == 1
    assert len(result.interaction_outcomes) == 3
    assert all(item.command_accepted for item in result.interaction_outcomes)
    assert [item.transition_arrived for item in result.interaction_outcomes] == [False, False, True]
    assert result.interaction_outcomes[0].effect_observed is True
    assert result.interaction_outcomes[0].postcondition_satisfied is False
    assert result.ready_capture_id == "capture-ready"
    assert result.evidence_capture_ids == (
        "capture-1",
        "capture-2",
        "capture-3",
        "capture-clear",
        "capture-ready",
    )
    assert [intent.action_type.value for intent in coordinator.intents] == [
        "click",
        "click",
        "click",
        "observe",
    ]
    assert all("selector" not in intent.parameters for intent in coordinator.intents)
    assert coordinator.intents[-1].parameters["web_capture_stage"] == ("web_page_ready_baseline")


@pytest.mark.asyncio
async def test_successful_last_close_uses_clear_result_even_if_assessor_reports_floating_ad() -> (
    None
):
    """最后一层已关闭时，非阻断浮动广告不得把 clear 结果退回弹窗阻断。"""

    obstructed = _evidence("capture-obstructed", text="关闭广告")
    clear = _evidence("capture-clear", text=None)
    ready = _evidence("capture-ready", text=None)
    expanded_fact_ids = (*obstructed.interaction_facts.business_fact_identities, "e" * 64)
    clear = replace(
        clear,
        interaction_facts=replace(
            clear.interaction_facts,
            content_identity="c" * 64,
            business_fact_identities=expanded_fact_ids,
        ),
    )
    ready = replace(
        ready,
        interaction_facts=replace(
            ready.interaction_facts,
            content_identity="c" * 64,
            business_fact_identities=expanded_fact_ids,
        ),
    )

    class FloatingAdAssessor:
        async def assess_obstruction(self, **kwargs):
            if kwargs["screenshot_artifact_id"] == obstructed.screenshot_artifact_id:
                return WebObstructionAssessment(
                    obstruction_present=True,
                    dismiss_candidate_id=obstructed.candidates[0].candidate_id,
                    confidence=0.99,
                    reason="中央弹窗阻断主体",
                    human_verification_present=False,
                    age_gate_present=False,
                )
            raise WebObstructionReviewRequiredError(
                "模型把右下角浮动广告误报为阻断，但未能绑定关闭候选",
                reason_code="WEB_OBSTRUCTION_CANDIDATES_MISSING",
            )

    coordinator = Coordinator(["capture-clear", "capture-ready"])
    result = await WebPageReadinessCoordinator(
        assessor=FloatingAdAssessor(),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(
            {item.capture_id: item for item in (obstructed, clear, ready)}
        ),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id=obstructed.capture_id, page_role="home")

    assert result.interaction_outcomes[0].command_accepted is True
    assert result.interaction_outcomes[0].effect_observed is True
    assert result.interaction_outcomes[0].postcondition_satisfied is True
    assert result.status is WebReadinessStatus.STABLE
    assert result.ready_capture_id == "capture-ready"
    assert [intent.action_type.value for intent in coordinator.intents] == [
        "click",
        "observe",
    ]


def test_deterministic_clear_rejects_unsealed_or_drifted_result() -> None:
    """确定性 clear 不能越过授权、原层、主体身份或会话边界。"""

    obstructed = _evidence("capture-obstructed", text="关闭广告")
    clear = _evidence("capture-clear", text=None)
    outcome = WebInteractionOutcome(
        command_accepted=True,
        effect_observed=True,
        postcondition_satisfied=True,
        transition_arrived=False,
        after_capture_id=clear.capture_id,
        failure_reason=None,
    )
    pending_close = (object(), "#close", obstructed, True)

    assert not _is_deterministically_clear_after_dismissal(
        current=clear,
        pending_close=None,
        outcomes=[outcome],
    )
    assert not _is_deterministically_clear_after_dismissal(
        current=clear,
        pending_close=(*pending_close[:3], False),
        outcomes=[outcome],
    )
    assert not _is_deterministically_clear_after_dismissal(
        current=obstructed,
        pending_close=pending_close,
        outcomes=[replace(outcome, after_capture_id=obstructed.capture_id)],
    )
    assert not _is_deterministically_clear_after_dismissal(
        current=replace(
            clear,
            interaction_facts=replace(
                clear.interaction_facts,
                content_identity="changed-business-subject",
                business_fact_identities=("c" * 64,),
            ),
        ),
        pending_close=pending_close,
        outcomes=[outcome],
    )
    assert not _is_deterministically_clear_after_dismissal(
        current=replace(clear, session_id="web:other-run"),
        pending_close=pending_close,
        outcomes=[outcome],
    )
    newly_obstructed_facts = web_capture_content_facts(
        {
            "elements": [
                {"tag": "main", "selector": "#business", "visible": True, "text": "原业务正文"},
                {
                    "tag": "section",
                    "selector": "#new-modal",
                    "visible": True,
                    "web_obstruction_layer": "#new-modal",
                    "web_obstruction_layer_structural": True,
                },
            ]
        },
        url=clear.interaction_facts.url,
    )
    assert not _is_deterministically_clear_after_dismissal(
        current=replace(clear, interaction_facts=newly_obstructed_facts),
        pending_close=pending_close,
        outcomes=[outcome],
    )


@pytest.mark.asyncio
async def test_readiness_intent_id_fits_database_limit_and_keeps_raw_ids() -> None:
    source_capture_id = "123e4567-e89b-12d3-a456-426614174000"
    evidence = [
        _evidence(source_capture_id, text="关闭广告"),
        _evidence("capture-clear", text=None),
        _evidence("capture-ready", text=None),
    ]
    by_id = {item.capture_id: item for item in evidence}
    coordinator = Coordinator(["capture-clear", "capture-ready"])

    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({item.screenshot_artifact_id: item for item in evidence}),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(by_id),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id=source_capture_id, page_role="menu")

    assert result.status is WebReadinessStatus.STABLE
    close_intent = coordinator.intents[0]
    assert len(close_intent.intent_id) <= 128
    assert close_intent.parameters["web_sealed_source_capture_id"] == source_capture_id
    assert str(close_intent.parameters["web_sealed_candidate_id"]).startswith("web-candidate:")


@pytest.mark.asyncio
async def test_no_popup_creates_ready_baseline_without_click() -> None:
    evidence = [
        _evidence("capture-1", text=None),
        _evidence("capture-ready", text=None),
    ]
    by_id = {item.capture_id: item for item in evidence}
    coordinator = Coordinator(["capture-ready"])

    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({item.screenshot_artifact_id: item for item in evidence}),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(by_id),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id="capture-1", page_role="home")

    assert result.status is WebReadinessStatus.STABLE
    assert result.dismissed_layers == ()
    assert [intent.action_type.value for intent in coordinator.intents] == ["observe"]
    expected_digest = hashlib.sha256(b"capture-1:observe:ready").hexdigest()
    assert coordinator.intents[0].intent_id == f"web-readiness:observe:{expected_digest}"


@pytest.mark.asyncio
async def test_popup_auto_disappearing_before_click_rechecks_fresh_capture() -> None:
    evidence = [
        _evidence("capture-1", text="关闭广告"),
        _evidence("capture-fresh", text=None),
        _evidence("capture-ready", text=None),
    ]
    by_id = {item.capture_id: item for item in evidence}

    class StaleThenObserveCoordinator:
        def __init__(self) -> None:
            self.intents = []

        async def coordinate(self, command):
            self.intents.append(command.intent)
            if command.intent.action_type.value == "click":
                raise stale_web_candidate_error(command.intent)
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id="capture-ready"),
                interaction=SimpleNamespace(execution_outcome="succeeded", status="succeeded"),
            )

    coordinator = StaleThenObserveCoordinator()
    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({item.screenshot_artifact_id: item for item in evidence}),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(by_id),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id="capture-1", page_role="menu")

    assert result.status is WebReadinessStatus.STABLE
    assert result.dismissed_layers == ()
    assert result.ready_capture_id == "capture-ready"
    assert result.evidence_capture_ids == (
        "capture-1",
        "capture-fresh",
        "capture-ready",
    )
    assert [intent.action_type.value for intent in coordinator.intents] == [
        "click",
        "observe",
    ]


@pytest.mark.asyncio
async def test_popup_stale_recovery_rejects_fresh_capture_from_another_session() -> None:
    evidence = [
        _evidence("capture-1", text="关闭广告"),
        replace(
            _evidence("capture-fresh", text=None),
            session_id="web:other-run",
        ),
    ]
    by_id = {item.capture_id: item for item in evidence}

    class CoordinatorWithForeignFreshCapture:
        async def coordinate(self, command):
            if command.intent.action_type.value == "click":
                raise stale_web_candidate_error(command.intent)
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id="capture-ready"),
                interaction=SimpleNamespace(execution_outcome="succeeded", status="succeeded"),
            )

    with pytest.raises(CaptureCoordinationError):
        await WebPageReadinessCoordinator(
            assessor=ScriptedAssessor({item.screenshot_artifact_id: item for item in evidence}),
            coordinator=CoordinatorWithForeignFreshCapture(),
            evidence_reader=EvidenceReader(by_id),
            sleeper=lambda _seconds: _completed(),
        ).prepare(source_capture_id="capture-1", page_role="menu")


@pytest.mark.asyncio
async def test_no_popup_allows_desktop_capture_and_llm_latency() -> None:
    evidence = [
        _evidence("capture-1", text=None),
        _evidence("capture-ready", text=None),
    ]
    by_id = {item.capture_id: item for item in evidence}
    coordinator = Coordinator(["capture-ready"])
    readings = iter((0.0, 0.0, 30.0, 80.0, 90.0))

    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({item.screenshot_artifact_id: item for item in evidence}),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(by_id),
        sleeper=lambda _seconds: _completed(),
        monotonic=lambda: next(readings),
    ).prepare(source_capture_id="capture-1", page_role="home")

    assert result.status is WebReadinessStatus.STABLE
    assert result.ready_capture_id == "capture-ready"


@pytest.mark.asyncio
async def test_ready_baseline_requires_stable_classification_evidence() -> None:
    entry = _evidence("capture-entry", text=None)
    loading = _evidence(
        "capture-loading",
        text=None,
        readiness=_settle_assessment(status=WebReadinessStatus.LOADING),
    )
    coordinator = Coordinator([loading.capture_id])

    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({entry.screenshot_artifact_id: entry}),
        coordinator=coordinator,
        evidence_reader=EvidenceReader({entry.capture_id: entry, loading.capture_id: loading}),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id=entry.capture_id, page_role="menu")

    assert result.status is WebReadinessStatus.LOADING
    assert result.ready_capture_id is None
    assert result.role is WebEvidenceRole.CLASSIFICATION
    assert result.signals is not None
    assert result.signals.visible_loader is True
    assert result.identity_before == "https://example.test/detail/1"
    assert result.identity_after == "https://example.test/detail/1"
    assert result.network_capability is WebNetworkCapability.UNKNOWN
    assert result.evidence_capture_ids == (entry.capture_id, loading.capture_id)


@pytest.mark.asyncio
async def test_stable_baseline_returns_explicit_role_and_dynamic_signals() -> None:
    entry = _evidence("capture-entry", text=None)
    stable = _evidence(
        "capture-stable",
        text=None,
        readiness=_settle_assessment(),
    )

    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({entry.screenshot_artifact_id: entry}),
        coordinator=Coordinator([stable.capture_id]),
        evidence_reader=EvidenceReader({entry.capture_id: entry, stable.capture_id: stable}),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id=entry.capture_id, page_role="detail")

    assert result.status is WebReadinessStatus.STABLE
    assert result.ready_capture_id == stable.capture_id
    assert result.role is WebEvidenceRole.CLASSIFICATION
    assert result.missing_signals == ()
    assert result.dynamic_reasons == ("network_capability_unknown",)


@pytest.mark.asyncio
async def test_stable_frame_with_wrong_role_cannot_serve_classification() -> None:
    entry = _evidence("capture-entry", text=None)
    inspection = _evidence(
        "capture-inspection",
        text=None,
        readiness=_settle_assessment(role=WebEvidenceRole.INSPECTION),
    )

    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({entry.screenshot_artifact_id: entry}),
        coordinator=Coordinator([inspection.capture_id]),
        evidence_reader=EvidenceReader(
            {entry.capture_id: entry, inspection.capture_id: inspection}
        ),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id=entry.capture_id, page_role="detail")

    assert result.status is WebReadinessStatus.CAPTURE_FAILED
    assert result.ready_capture_id is None
    assert result.blocking_reasons == ("evidence_role_mismatch",)


@pytest.mark.asyncio
async def test_unchanged_obstruction_signature_stops_current_page() -> None:
    evidence = [
        _evidence("capture-1", text="关闭广告"),
        _evidence("capture-2", text="关闭广告"),
    ]
    by_id = {item.capture_id: item for item in evidence}
    coordinator = Coordinator(["capture-2"])

    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({item.screenshot_artifact_id: item for item in evidence}),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(by_id),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id="capture-1", page_role="detail")

    assert result.status == "popup_blocked"
    assert len(coordinator.intents) == 1
    assert result.ready_capture_id is None


@pytest.mark.asyncio
async def test_same_popup_selector_with_rotated_creative_continues_until_clear() -> None:
    """同一关闭控件下的创意哈希变化表示弹层已前进，不得误判为 no-op。"""

    def creative_fingerprint(sha256: str) -> str | None:
        snapshot = {
            "elements": [
                {
                    "selector": "#close",
                    "tag": "button",
                    "text": "关闭广告",
                    "visible": True,
                    "web_obstruction_layer": "#page-modal",
                    "web_obstruction_layer_structural": True,
                    "class_tokens": ["modal-close"],
                    "rect": {"x": 1800, "y": 30, "width": 50, "height": 50},
                }
            ],
            "resource_summaries": [
                {
                    "selector": "#page-modal > div > img",
                    "attribute": "src",
                    "sha256": sha256,
                }
            ],
        }
        return web_obstruction_state_fingerprint(snapshot)

    first = replace(
        _evidence("capture-1", text="关闭广告"),
        obstruction_state_fingerprint=creative_fingerprint("a" * 64),
    )
    second = replace(
        _evidence("capture-2", text="关闭广告"),
        obstruction_state_fingerprint=creative_fingerprint("b" * 64),
    )
    clear = _evidence("capture-clear", text=None)
    ready = _evidence("capture-ready", text=None)
    evidence = (first, second, clear, ready)
    coordinator = Coordinator([second.capture_id, clear.capture_id, ready.capture_id])

    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({item.screenshot_artifact_id: item for item in evidence}),
        coordinator=coordinator,
        evidence_reader=EvidenceReader({item.capture_id: item for item in evidence}),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id=first.capture_id, page_role="home")

    assert result.status is WebReadinessStatus.STABLE
    assert result.ready_capture_id == ready.capture_id
    assert [item.action_type.value for item in coordinator.intents] == [
        "click",
        "click",
        "observe",
    ]


@pytest.mark.asyncio
async def test_same_popup_selector_and_creative_hash_remains_blocked() -> None:
    """关闭后 selector、弹层和创意哈希均未变化时仍按 no-op 失败关闭。"""

    first = replace(
        _evidence("capture-1", text="关闭广告"),
        obstruction_state_fingerprint="a" * 64,
    )
    second = replace(
        _evidence("capture-2", text="关闭广告"),
        obstruction_state_fingerprint="a" * 64,
    )
    coordinator = Coordinator([second.capture_id])

    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor(
            {
                first.screenshot_artifact_id: first,
                second.screenshot_artifact_id: second,
            }
        ),
        coordinator=coordinator,
        evidence_reader=EvidenceReader({first.capture_id: first, second.capture_id: second}),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id=first.capture_id, page_role="home")

    assert result.status == "popup_blocked"
    assert result.ready_capture_id is None
    assert [item.action_type.value for item in coordinator.intents] == ["click"]


@pytest.mark.asyncio
async def test_unchanged_obstruction_reasks_model_with_untried_safe_candidate() -> None:
    """首个关闭控件无效时，同一阻断层仍可由模型选择另一个未尝试候选。"""

    def evidence(capture_id: str, *, clear: bool = False) -> WebPageEvidence:
        snapshot = {
            "elements": [
                {
                    "selector": selector,
                    "tag": "button",
                    "role": "button",
                    "text": "关闭",
                    "class_tokens": ["modal-close"],
                    "ancestor_class_tokens": ["modal"],
                    "web_obstruction_layer": "#page-modal",
                    "web_obstruction_layer_kind": "modal",
                    "web_obstruction_layer_structural": True,
                    "web_obstruction_close_semantics": True,
                    "within_web_navigation": False,
                    "rect": {"x": x, "y": 30, "width": 50, "height": 50},
                    "interactive": True,
                    "visible": True,
                    "effective_z_index": 900,
                    "hit_test": {"center_hits_target": True},
                }
                for selector, x in (("#decorative-close", 1740), ("#real-close", 1810))
            ]
            if not clear
            else []
        }
        snapshot["elements"].append(
            {"tag": "main", "selector": "#business", "visible": True, "text": "原业务正文"}
        )
        readiness = _settle_assessment() if capture_id.endswith("-ready") else None
        return WebPageEvidence(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            capture_id=capture_id,
            screenshot_artifact_id=f"{capture_id}:screenshot",
            dom_artifact_id=f"{capture_id}:dom",
            candidates=build_web_obstruction_candidates(
                capture_id=capture_id,
                ui_snapshot=snapshot,
            ),
            deadline=datetime.now(UTC) + timedelta(minutes=2),
            obstruction_state_fingerprint="a" * 64 if not clear else None,
            readiness=readiness,
            interaction_facts=replace(
                web_capture_content_facts(snapshot, url="https://example.test/"),
                identity_verified=True,
                readiness_accepted=bool(readiness and readiness.can_serve_evidence),
            ),
        )

    first = evidence("capture-1")
    unchanged = evidence("capture-2")
    clear = evidence("capture-clear", clear=True)
    ready = evidence("capture-ready", clear=True)
    assessor_calls: list[tuple[str, ...]] = []

    class FirstAvailableAssessor:
        async def assess_obstruction(self, **kwargs):
            candidate_ids = tuple(item["candidate_id"] for item in kwargs["candidates"])
            assessor_calls.append(candidate_ids)
            if candidate_ids:
                return WebObstructionAssessment(
                    obstruction_present=True,
                    dismiss_candidate_id=candidate_ids[0],
                    confidence=0.99,
                    reason="模型选择当前未尝试的关闭候选",
                    human_verification_present=False,
                    age_gate_present=False,
                )
            return WebObstructionAssessment(
                obstruction_present=False,
                dismiss_candidate_id=None,
                confidence=0.99,
                reason="页面无遮挡",
                human_verification_present=False,
                age_gate_present=False,
            )

    coordinator = Coordinator([unchanged.capture_id, clear.capture_id, ready.capture_id])
    result = await WebPageReadinessCoordinator(
        assessor=FirstAvailableAssessor(),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(
            {item.capture_id: item for item in (first, unchanged, clear, ready)}
        ),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id=first.capture_id, page_role="home")

    assert result.status is WebReadinessStatus.STABLE
    assert result.ready_capture_id == ready.capture_id
    assert [item.action_type.value for item in coordinator.intents] == [
        "click",
        "click",
        "observe",
    ]
    assert len(assessor_calls) == 3
    assert len(assessor_calls[0]) == 2
    assert len(assessor_calls[1]) == 1
    assert assessor_calls[1][0] != assessor_calls[0][0]


@pytest.mark.asyncio
async def test_small_ordinary_overlay_skips_model_and_keeps_home_ready() -> None:
    entry = replace(
        _evidence("capture-1", text="关闭浮层"),
        benign_ordinary_overlays_only=True,
    )
    ready = _evidence("capture-ready", text="关闭浮层")

    class AssessorMustNotRun:
        async def assess_obstruction(self, **_kwargs):
            raise AssertionError("小于主体 30% 的普通浮层不得调用模型")

    coordinator = Coordinator([ready.capture_id])
    result = await WebPageReadinessCoordinator(
        assessor=AssessorMustNotRun(),
        coordinator=coordinator,
        evidence_reader=EvidenceReader({entry.capture_id: entry, ready.capture_id: ready}),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id=entry.capture_id, page_role="home")

    assert result.status is WebReadinessStatus.STABLE
    assert result.ready_capture_id == ready.capture_id
    assert [item.action_type.value for item in coordinator.intents] == ["observe"]


@pytest.mark.asyncio
async def test_model_confirmed_obstruction_uses_unique_safe_close_even_at_low_confidence() -> None:
    entry = _evidence("capture-1", text="关闭弹窗")
    clear = _evidence("capture-clear", text=None)
    ready = _evidence("capture-ready", text=None)

    class LowConfidenceAssessor:
        async def assess_obstruction(self, **kwargs):
            candidates = kwargs["candidates"]
            if candidates:
                return WebObstructionAssessment(
                    obstruction_present=True,
                    dismiss_candidate_id=candidates[0]["candidate_id"],
                    confidence=0.65,
                    reason="模型确认普通阻断，但视觉内容使置信度偏低",
                    human_verification_present=False,
                    age_gate_present=False,
                )
            return WebObstructionAssessment(
                obstruction_present=False,
                dismiss_candidate_id=None,
                confidence=0.99,
                reason="阻断已消失",
                human_verification_present=False,
                age_gate_present=False,
            )

    coordinator = Coordinator([clear.capture_id, ready.capture_id])
    result = await WebPageReadinessCoordinator(
        assessor=LowConfidenceAssessor(),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(
            {item.capture_id: item for item in (entry, clear, ready)}
        ),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id=entry.capture_id, page_role="home")

    assert result.status is WebReadinessStatus.STABLE
    assert [item.action_type.value for item in coordinator.intents] == ["click", "observe"]


@pytest.mark.asyncio
async def test_same_shape_different_obstruction_candidates_continue() -> None:
    evidence = [
        _evidence("capture-1", text="关闭", selector="#first-ad .close"),
        _evidence("capture-2", text="关闭", selector="#second-ad .close"),
        _evidence("capture-clear", text=None),
        _evidence("capture-ready", text=None),
    ]
    by_id = {item.capture_id: item for item in evidence}
    coordinator = Coordinator(["capture-2", "capture-clear", "capture-ready"])

    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({item.screenshot_artifact_id: item for item in evidence}),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(by_id),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id="capture-1", page_role="home")

    assert result.status is WebReadinessStatus.STABLE
    assert len(result.dismissed_layers) == 1
    assert [intent.action_type.value for intent in coordinator.intents] == [
        "click",
        "click",
        "observe",
    ]


@pytest.mark.asyncio
async def test_same_close_control_continues_when_obstruction_content_changes() -> None:
    evidence = [
        _evidence(
            "capture-1",
            text="关闭",
            obstruction_state_fingerprint="1" * 64,
        ),
        _evidence(
            "capture-2",
            text="关闭",
            obstruction_state_fingerprint="2" * 64,
        ),
        _evidence("capture-clear", text=None),
        _evidence("capture-ready", text=None),
    ]
    by_id = {item.capture_id: item for item in evidence}
    coordinator = Coordinator(["capture-2", "capture-clear", "capture-ready"])

    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({item.screenshot_artifact_id: item for item in evidence}),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(by_id),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id="capture-1", page_role="home")

    assert result.status is WebReadinessStatus.STABLE
    assert len(result.dismissed_layers) == 1
    assert [intent.action_type.value for intent in coordinator.intents] == [
        "click",
        "click",
        "observe",
    ]


@pytest.mark.asyncio
async def test_hidden_age_gate_dom_cannot_block_unique_ordinary_dismissal() -> None:
    entry = _evidence("capture-1", text="关闭")
    second = _evidence("capture-2", text="关闭", selector="#close-2")
    clear = _evidence("capture-clear", text=None)
    ready = _evidence("capture-ready", text=None)

    class HiddenAgeGateResidueAssessor(ScriptedAssessor):
        async def assess_obstruction(self, **kwargs):
            if kwargs["screenshot_artifact_id"] in {
                entry.screenshot_artifact_id,
                second.screenshot_artifact_id,
            }:
                return WebObstructionAssessment(
                    obstruction_present=True,
                    dismiss_candidate_id=None,
                    confidence=0.96,
                    reason="DOM 残留年龄门禁节点",
                    human_verification_present=False,
                    age_gate_present=True,
                )
            return await super().assess_obstruction(**kwargs)

    age_handler = ScriptedAgeGateHandler(
        [
            SimpleNamespace(
                gate_present=False,
                clicked_candidate_id=None,
                after_capture_id=None,
                gate_disappeared=True,
            )
        ]
    )
    coordinator = Coordinator(["capture-2", "capture-clear", "capture-ready"])
    subject = WebPageReadinessCoordinator(
        assessor=HiddenAgeGateResidueAssessor(
            {
                second.screenshot_artifact_id: second,
                clear.screenshot_artifact_id: clear,
                ready.screenshot_artifact_id: ready,
            }
        ),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(
            {
                entry.capture_id: entry,
                second.capture_id: second,
                clear.capture_id: clear,
                ready.capture_id: ready,
            }
        ),
        age_gate_handler=age_handler,
        adult_self_attestation=_adult_self_attestation(),
        sleeper=lambda _seconds: _completed(),
    )

    result = await subject.prepare(
        source_capture_id="capture-1",
        page_role="home",
        age_gate_verified_absent_capture_id="capture-1",
    )

    assert result.status is WebReadinessStatus.STABLE
    assert result.ready_capture_id == "capture-ready"
    assert [intent.action_type.value for intent in coordinator.intents] == [
        "click",
        "click",
        "observe",
    ]
    assert age_handler.calls == []


@pytest.mark.asyncio
async def test_adult_attestation_rechecks_visual_age_false_positive() -> None:
    """无结构门禁且业务层清晰时，成年声明只允许经独立补采后消除视觉误报。"""

    entry = _evidence("capture-1", text=None)
    refreshed = _evidence("capture-refreshed", text=None)
    ready = _evidence("capture-ready", text=None)

    class VisualAgeFalsePositiveAssessor(ScriptedAssessor):
        async def assess_obstruction(self, **kwargs):
            if kwargs["screenshot_artifact_id"] in {
                entry.screenshot_artifact_id,
                refreshed.screenshot_artifact_id,
            }:
                return WebObstructionAssessment(
                    obstruction_present=True,
                    dismiss_candidate_id=None,
                    confidence=0.91,
                    reason="页面正文包含年龄提示字样，但没有结构化门禁控件",
                    human_verification_present=False,
                    age_gate_present=True,
                )
            return await super().assess_obstruction(**kwargs)

    coordinator = Coordinator(["capture-refreshed", "capture-ready"])
    age_handler = ScriptedAgeGateHandler([])
    result = await WebPageReadinessCoordinator(
        assessor=VisualAgeFalsePositiveAssessor({ready.screenshot_artifact_id: ready}),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(
            {
                entry.capture_id: entry,
                refreshed.capture_id: refreshed,
                ready.capture_id: ready,
            }
        ),
        age_gate_handler=age_handler,
        adult_self_attestation=_adult_self_attestation(),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id=entry.capture_id, page_role="menu")

    assert result.status is WebReadinessStatus.STABLE
    assert result.ready_capture_id == "capture-ready"
    assert result.evidence_capture_ids == (
        "capture-1",
        "capture-refreshed",
        "capture-ready",
    )
    assert [intent.action_type.value for intent in coordinator.intents] == [
        "observe",
        "observe",
    ]
    assert age_handler.calls == []


@pytest.mark.asyncio
async def test_visual_age_gate_without_adult_attestation_stays_blocked() -> None:
    """没有成年声明时，相同的视觉年龄门禁判定不能被结构证据降级。"""

    entry = _evidence("capture-1", text=None)

    class VisualAgeAssessor:
        async def assess_obstruction(self, **kwargs):
            del kwargs
            return WebObstructionAssessment(
                obstruction_present=True,
                dismiss_candidate_id=None,
                confidence=0.91,
                reason="视觉证据疑似年龄门禁",
                human_verification_present=False,
                age_gate_present=True,
            )

    class BlockingAgeHandler:
        async def handle(self, evidence, *, adult_self_attestation):
            del evidence, adult_self_attestation
            raise WebAgeGateReviewRequiredError("缺少成年声明，禁止继续")

    coordinator = Coordinator([])
    result = await WebPageReadinessCoordinator(
        assessor=VisualAgeAssessor(),
        coordinator=coordinator,
        evidence_reader=EvidenceReader({entry.capture_id: entry}),
        age_gate_handler=BlockingAgeHandler(),
        adult_self_attestation=None,
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id=entry.capture_id, page_role="menu")

    assert result.status is WebReadinessStatus.POPUP_BLOCKED
    assert coordinator.intents == []


@pytest.mark.asyncio
async def test_verified_no_age_gate_allows_low_confidence_unique_rotating_popup() -> None:
    first = _evidence(
        "capture-1",
        text="关闭",
        obstruction_state_fingerprint="1" * 64,
    )
    second = _evidence(
        "capture-2",
        text="关闭",
        selector="#rotating-ad-2 .close",
        obstruction_state_fingerprint="2" * 64,
    )
    clear = _evidence("capture-clear", text=None)
    ready = _evidence("capture-ready", text=None)

    class LowConfidenceRotatingPopupAssessor(ScriptedAssessor):
        async def assess_obstruction(self, **kwargs):
            if kwargs["screenshot_artifact_id"] in {
                first.screenshot_artifact_id,
                second.screenshot_artifact_id,
            }:
                return WebObstructionAssessment(
                    obstruction_present=True,
                    dismiss_candidate_id=None,
                    confidence=0.62,
                    reason="截图存在轮换广告，但模型对广告内容识别置信度较低",
                    human_verification_present=False,
                    age_gate_present=True,
                )
            return await super().assess_obstruction(**kwargs)

    coordinator = Coordinator(["capture-2", "capture-clear", "capture-ready"])
    age_handler = ScriptedAgeGateHandler([])
    result = await WebPageReadinessCoordinator(
        assessor=LowConfidenceRotatingPopupAssessor(
            {
                clear.screenshot_artifact_id: clear,
                ready.screenshot_artifact_id: ready,
            }
        ),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(
            {
                first.capture_id: first,
                second.capture_id: second,
                clear.capture_id: clear,
                ready.capture_id: ready,
            }
        ),
        age_gate_handler=age_handler,
        adult_self_attestation=_adult_self_attestation(),
        sleeper=lambda _seconds: _completed(),
    ).prepare(
        source_capture_id=first.capture_id,
        page_role="home",
        age_gate_verified_absent_capture_id=first.capture_id,
    )

    assert result.status is WebReadinessStatus.STABLE
    assert result.ready_capture_id == "capture-ready"
    assert [intent.action_type.value for intent in coordinator.intents] == [
        "click",
        "click",
        "observe",
    ]
    assert age_handler.calls == []


@pytest.mark.asyncio
async def test_retryable_click_timeout_uses_read_only_probe_before_continuing() -> None:
    entry = _evidence("capture-1", text="关闭")
    timeout_before = _evidence("capture-timeout-before", text="6秒后将会自动关闭")
    clear = _evidence("capture-timeout-probe", text=None)
    ready = _evidence("capture-ready", text=None)

    class TimeoutThenObserveCoordinator(Coordinator):
        async def coordinate(self, command):
            self.commands.append(command)
            self.intents.append(command.intent)
            if len(self.intents) == 1:
                raise retryable_web_action_timeout_error(command.intent)
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id=next(self.capture_ids)),
                interaction=SimpleNamespace(execution_outcome="succeeded", status="succeeded"),
            )

    coordinator = TimeoutThenObserveCoordinator([clear.capture_id, ready.capture_id])
    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor(
            {
                entry.screenshot_artifact_id: entry,
                clear.screenshot_artifact_id: clear,
                ready.screenshot_artifact_id: ready,
            }
        ),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(
            {
                entry.capture_id: entry,
                timeout_before.capture_id: timeout_before,
                clear.capture_id: clear,
                ready.capture_id: ready,
            }
        ),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id=entry.capture_id, page_role="home")

    assert result.status is WebReadinessStatus.STABLE
    assert result.ready_capture_id == ready.capture_id
    assert result.evidence_capture_ids == (
        entry.capture_id,
        timeout_before.capture_id,
        clear.capture_id,
        ready.capture_id,
    )
    assert [intent.action_type.value for intent in coordinator.intents] == [
        "click",
        "observe",
        "observe",
    ]


@pytest.mark.asyncio
async def test_menu_age_gate_uses_specialized_handler_before_readiness() -> None:
    snapshot = {
        "elements": [
            {
                "selector": "#age-gate-enter",
                "tag": "button",
                "role": "button",
                "text": "已满 18 岁并进入",
                "class_tokens": ["age-gate-enter"],
                "ancestor_class_tokens": ["age-gate"],
                "web_obstruction_layer": "#age-gate",
                "web_obstruction_layer_kind": "age_gate",
                "web_obstruction_layer_structural": True,
                "web_obstruction_close_semantics": True,
                "within_web_navigation": False,
                "rect": {"x": 800, "y": 600, "width": 320, "height": 64},
                "interactive": True,
                "visible": True,
                "effective_z_index": 1000,
                "hit_test": {"center_hits_target": True},
            }
        ]
    }
    entry = WebPageEvidence(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="capture-gate",
        screenshot_artifact_id="capture-gate:screenshot",
        dom_artifact_id="capture-gate:dom",
        candidates=build_web_obstruction_candidates(
            capture_id="capture-gate",
            ui_snapshot=snapshot,
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )
    after_gate = _evidence("capture-after-gate", text=None)
    ready = _evidence("capture-ready", text=None)
    age_handler = ScriptedAgeGateHandler(
        [
            SimpleNamespace(
                gate_present=True,
                clicked_candidate_id=entry.candidates[0].candidate_id,
                after_capture_id=after_gate.capture_id,
                gate_disappeared=True,
            )
        ]
    )
    coordinator = Coordinator([ready.capture_id])
    subject = WebPageReadinessCoordinator(
        assessor=ScriptedAssessor(
            {
                entry.screenshot_artifact_id: entry,
                after_gate.screenshot_artifact_id: after_gate,
                ready.screenshot_artifact_id: ready,
            }
        ),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(
            {
                entry.capture_id: entry,
                after_gate.capture_id: after_gate,
                ready.capture_id: ready,
            }
        ),
        age_gate_handler=age_handler,
        adult_self_attestation=_adult_self_attestation(),
        sleeper=lambda _seconds: _completed(),
    )

    result = await subject.prepare(
        source_capture_id=entry.capture_id,
        page_role="menu",
    )

    assert entry.candidates[0].capability == "web_age_gate_confirm"
    assert result.status is WebReadinessStatus.STABLE
    assert result.evidence_capture_ids == (
        entry.capture_id,
        after_gate.capture_id,
        ready.capture_id,
    )
    assert [intent.action_type.value for intent in coordinator.intents] == ["observe"]
    assert age_handler.calls[0][1] == _adult_self_attestation()


@pytest.mark.asyncio
async def test_human_verification_stops_without_click() -> None:
    entry = _evidence("capture-1", text="关闭广告")

    class HumanVerificationAssessor:
        async def assess_obstruction(self, **kwargs):
            del kwargs
            return WebObstructionAssessment(
                obstruction_present=True,
                dismiss_candidate_id=None,
                confidence=0.99,
                reason="检测到人机验证",
                human_verification_present=True,
                age_gate_present=False,
            )

    coordinator = Coordinator([])
    result = await WebPageReadinessCoordinator(
        assessor=HumanVerificationAssessor(),
        coordinator=coordinator,
        evidence_reader=EvidenceReader({"capture-1": entry}),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id="capture-1", page_role="menu")

    assert result.status == "human_verification_blocked"
    assert coordinator.intents == []


@pytest.mark.asyncio
async def test_unresolved_ordinary_obstruction_stops_after_single_probe() -> None:
    entry = _evidence("capture-1", text=None)
    refreshed = _evidence("capture-refreshed", text=None)

    class UnresolvedObstructionAssessor:
        async def assess_obstruction(self, **kwargs):
            del kwargs
            raise WebObstructionReviewRequiredError("存在普通阻断，但当前 Capture 无安全关闭候选。")

    coordinator = Coordinator(["capture-refreshed"])
    result = await WebPageReadinessCoordinator(
        assessor=UnresolvedObstructionAssessor(),
        coordinator=coordinator,
        evidence_reader=EvidenceReader({"capture-1": entry, "capture-refreshed": refreshed}),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id="capture-1", page_role="menu")

    assert result.status == "popup_blocked"
    assert result.ready_capture_id is None
    assert [intent.action_type.value for intent in coordinator.intents] == ["observe"]
    assert coordinator.intents[0].parameters["web_capture_stage"] == "web_page_probe"


@pytest.mark.asyncio
async def test_structured_assessment_error_retries_once_with_fresh_probe() -> None:
    entry = _evidence("capture-1", text=None)
    refreshed = _evidence("capture-refreshed", text=None)
    ready = _evidence("capture-ready", text=None)

    class InvalidFirstAssessment:
        def __init__(self) -> None:
            self.calls = 0

        async def assess_obstruction(self, **kwargs):
            del kwargs
            self.calls += 1
            if self.calls == 1:
                raise ValueError("模型返回的阻断判定不是合法结构")
            return WebObstructionAssessment(
                obstruction_present=False,
                dismiss_candidate_id=None,
                confidence=0.99,
                reason="页面无遮挡",
                human_verification_present=False,
                age_gate_present=False,
            )

    assessor = InvalidFirstAssessment()
    coordinator = Coordinator(["capture-refreshed", "capture-ready"])
    result = await WebPageReadinessCoordinator(
        assessor=assessor,
        coordinator=coordinator,
        evidence_reader=EvidenceReader(
            {
                entry.capture_id: entry,
                refreshed.capture_id: refreshed,
                ready.capture_id: ready,
            }
        ),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id="capture-1", page_role="detail")

    assert assessor.calls == 2
    assert result.status is WebReadinessStatus.STABLE
    assert result.ready_capture_id == "capture-ready"
    assert result.evidence_capture_ids == (
        "capture-1",
        "capture-refreshed",
        "capture-ready",
    )
    assert [intent.action_type.value for intent in coordinator.intents] == [
        "observe",
        "observe",
    ]
    assert [intent.parameters["web_capture_stage"] for intent in coordinator.intents] == [
        "web_page_probe",
        "web_page_ready_baseline",
    ]


@pytest.mark.asyncio
async def test_transient_assessment_service_error_retries_once_with_fresh_probe() -> None:
    entry = _evidence("capture-1", text=None)
    refreshed = _evidence("capture-refreshed", text=None)
    ready = _evidence("capture-ready", text=None)

    class UnavailableFirstAssessment:
        def __init__(self) -> None:
            self.calls = 0

        async def assess_obstruction(self, **kwargs):
            del kwargs
            self.calls += 1
            if self.calls == 1:
                raise WebAssessmentTransientServiceError(status_code=503)
            return WebObstructionAssessment(
                obstruction_present=False,
                dismiss_candidate_id=None,
                confidence=0.99,
                reason="页面无遮挡",
                human_verification_present=False,
                age_gate_present=False,
            )

    assessor = UnavailableFirstAssessment()
    coordinator = Coordinator(["capture-refreshed", "capture-ready"])
    result = await WebPageReadinessCoordinator(
        assessor=assessor,
        coordinator=coordinator,
        evidence_reader=EvidenceReader(
            {
                entry.capture_id: entry,
                refreshed.capture_id: refreshed,
                ready.capture_id: ready,
            }
        ),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id="capture-1", page_role="menu")

    assert assessor.calls == 2
    assert result.status is WebReadinessStatus.STABLE
    assert result.evidence_capture_ids == (
        "capture-1",
        "capture-refreshed",
        "capture-ready",
    )
    assert [intent.action_type.value for intent in coordinator.intents] == [
        "observe",
        "observe",
    ]


@pytest.mark.asyncio
async def test_structured_assessment_error_stops_after_single_fresh_probe() -> None:
    entry = _evidence("capture-1", text=None)
    refreshed = _evidence("capture-refreshed", text=None)

    class AlwaysInvalidAssessment:
        def __init__(self) -> None:
            self.calls = 0

        async def assess_obstruction(self, **kwargs):
            del kwargs
            self.calls += 1
            raise TypeError("模型返回的阻断判定字段类型错误")

    assessor = AlwaysInvalidAssessment()
    coordinator = Coordinator(["capture-refreshed"])
    result = await WebPageReadinessCoordinator(
        assessor=assessor,
        coordinator=coordinator,
        evidence_reader=EvidenceReader(
            {
                entry.capture_id: entry,
                refreshed.capture_id: refreshed,
            }
        ),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id="capture-1", page_role="detail")

    assert assessor.calls == 2
    assert result.status == "popup_blocked"
    assert result.ready_capture_id is None
    assert result.evidence_capture_ids == ("capture-1", "capture-refreshed")
    assert [intent.action_type.value for intent in coordinator.intents] == ["observe"]
    assert coordinator.intents[0].parameters["web_capture_stage"] == "web_page_probe"


@pytest.mark.asyncio
async def test_unresolved_popup_rechecks_when_cross_artifact_state_has_changed() -> None:
    blocked = _evidence("capture-1", text=None)
    clear = _evidence("capture-refreshed", text=None)
    ready = _evidence("capture-ready", text=None)

    class StaleFirstAssessor(ScriptedAssessor):
        async def assess_obstruction(self, **kwargs):
            if kwargs["screenshot_artifact_id"] == "capture-1:screenshot":
                raise WebObstructionReviewRequiredError(
                    "存在普通阻断，但当前 Capture 无安全关闭候选。"
                )
            return await super().assess_obstruction(**kwargs)

    coordinator = Coordinator(["capture-refreshed", "capture-ready"])
    result = await WebPageReadinessCoordinator(
        assessor=StaleFirstAssessor(
            {
                clear.screenshot_artifact_id: clear,
                ready.screenshot_artifact_id: ready,
            }
        ),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(
            {
                blocked.capture_id: blocked,
                clear.capture_id: clear,
                ready.capture_id: ready,
            }
        ),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id="capture-1", page_role="home")

    assert result.status is WebReadinessStatus.STABLE
    assert result.ready_capture_id == "capture-ready"
    assert result.evidence_capture_ids == (
        "capture-1",
        "capture-refreshed",
        "capture-ready",
    )
    assert [intent.action_type.value for intent in coordinator.intents] == [
        "observe",
        "observe",
    ]


@pytest.mark.asyncio
async def test_outside_closed_set_obstruction_stops_without_ready_observe() -> None:
    entry = _evidence("capture-1", text=None)
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-obstruction-assessment.v1",
                        "obstruction_present": True,
                        "dismiss_candidate_id": "web-candidate:invented",
                        "confidence": 0.91,
                        "reason": "截图存在阻断，但模型返回了候选闭集外 ID",
                        "human_verification_present": False,
                        "age_gate_present": False,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )

    class StaticEvidenceLoader:
        async def load(
            self,
            *,
            screenshot_artifact_id: str,
            dom_artifact_id: str,
            visible_text_artifact_id: str | None = None,
            supplemental_screenshot_artifact_ids: tuple[str, ...] = (),
        ) -> WebAssessmentEvidence:
            return WebAssessmentEvidence(
                screenshot_artifact_id=screenshot_artifact_id,
                screenshot_bytes=b"\x89PNG\r\n\x1a\nweb-readiness-test",
                screenshot_media_type="image/png",
                dom_artifact_id=dom_artifact_id,
                dom_text="<main>被普通阻断覆盖的页面</main>",
                visible_text_artifact_id=visible_text_artifact_id,
                visible_text=None,
            )

    coordinator = Coordinator([])
    result = await WebPageReadinessCoordinator(
        assessor=WebExplorationAssessmentAgent(
            client=client,
            evidence_loader=StaticEvidenceLoader(),
        ),
        coordinator=coordinator,
        evidence_reader=EvidenceReader({"capture-1": entry}),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id="capture-1", page_role="menu")

    assert result.status == "popup_blocked"
    assert result.ready_capture_id is None
    assert coordinator.commands == []


@pytest.mark.asyncio
async def test_floating_ad_without_structural_close_allows_ready_baseline() -> None:
    snapshot = {
        "elements": [
            {
                "selector": "#corner-floating-ad .close",
                "tag": "button",
                "role": "button",
                "text": "关闭广告",
                "class_tokens": ["close"],
                "ancestor_class_tokens": ["corner-floating-ad"],
                "web_obstruction_layer": None,
                "web_obstruction_layer_kind": None,
                "web_obstruction_layer_structural": False,
                "web_obstruction_close_semantics": True,
                "within_web_navigation": False,
                "rect": {"x": 1800, "y": 900, "width": 50, "height": 50},
                "interactive": True,
                "visible": True,
                "effective_z_index": 8,
                "hit_test": {"center_hits_target": True},
            }
        ]
    }
    entry = WebPageEvidence(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="capture-1",
        screenshot_artifact_id="capture-1:screenshot",
        dom_artifact_id="capture-1:dom",
        candidates=build_web_obstruction_candidates(
            capture_id="capture-1",
            ui_snapshot=snapshot,
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )
    coordinator = Coordinator(["capture-ready"])
    ready = _evidence("capture-ready", text=None)

    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({entry.screenshot_artifact_id: entry}),
        coordinator=coordinator,
        evidence_reader=EvidenceReader({"capture-1": entry, ready.capture_id: ready}),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id="capture-1", page_role="home")

    assert entry.candidates == ()
    assert result.status is WebReadinessStatus.STABLE
    assert [intent.action_type.value for intent in coordinator.intents] == ["observe"]


@pytest.mark.asyncio
async def test_twelve_dismissals_receive_final_clean_recheck() -> None:
    evidence = [_evidence(f"capture-{index}", text=f"关闭第 {index} 层") for index in range(1, 13)]
    evidence.extend(
        [
            _evidence("capture-clear", text=None),
            _evidence("capture-ready", text=None),
        ]
    )
    by_id = {item.capture_id: item for item in evidence}
    coordinator = Coordinator(
        [
            *(f"capture-{index}" for index in range(2, 13)),
            "capture-clear",
            "capture-ready",
        ]
    )

    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({item.screenshot_artifact_id: item for item in evidence}),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(by_id),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id="capture-1", page_role="menu")

    assert result.status is WebReadinessStatus.STABLE
    assert len(result.dismissed_layers) == 1
    assert len(result.interaction_outcomes) == 12
    assert result.ready_capture_id == "capture-ready"


def test_elapsed_budget_covers_all_layers_and_final_recheck() -> None:
    assert (
        WebPageReadinessCoordinator.MAX_ELAPSED_SECONDS
        >= (WebPageReadinessCoordinator.MAX_LAYERS + 1) * 60
    )


@pytest.mark.asyncio
async def test_hard_budget_expires_before_starting_next_async_step() -> None:
    entry = _evidence("capture-1", text=None)
    reader = EvidenceReader({"capture-1": entry})
    readings = iter((0.0, 901.0))
    coordinator = Coordinator([])

    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({entry.screenshot_artifact_id: entry}),
        coordinator=coordinator,
        evidence_reader=reader,
        sleeper=lambda _seconds: _completed(),
        monotonic=lambda: next(readings),
    ).prepare(source_capture_id="capture-1", page_role="home")

    assert result.status is WebReadinessStatus.TIMEOUT
    assert result.blocking_reasons == ("readiness_deadline_exceeded",)
    assert reader.calls == []
    assert coordinator.intents == []


async def _completed() -> None:
    return None
