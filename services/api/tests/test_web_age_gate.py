"""Web 成年门禁只读识别与密封候选点击闭环。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.capture_coordination import CaptureCoordinationError
from services.api.app.application.web_age_gate import (
    WebAdultAttestationRequiredError,
    WebAgeGateAssessment,
    WebAgeGateEvidence,
    WebAgeGateHandler,
    WebAgeGateReviewRequiredError,
    WebHumanVerificationBlockedError,
)
from services.api.app.domain.exploration import build_web_obstruction_candidates
from services.api.app.domain.task import AdultSelfAttestation
from services.api.tests.web_stale_candidate_test_support import (
    STRICT_STALE_MUTATIONS,
    stale_web_candidate_error,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence,declared,expected", [
    (0.69, True, WebAgeGateReviewRequiredError),
    (0.70, True, WebAgeGateReviewRequiredError),
    (0.75, True, WebAgeGateReviewRequiredError),
    (0.80, False, WebAdultAttestationRequiredError),
    (0.80, True, None),
])
async def test_age_gate_records_before_authorization_and_action(confidence, declared, expected):
    entry = _evidence(capture_id="capture-entry")
    calls = []

    class Assessor:
        async def assess(self, **kwargs):
            present = kwargs["screenshot_artifact_id"].startswith("capture-entry")
            return WebAgeGateAssessment(
                gate_present=present, confidence=confidence,
                confirmation_candidate_id=entry.candidates[0].candidate_id if present else None,
                human_verification_present=False, reason="年龄门可见" if present else "门禁已消失",
            )

    class Recorder:
        async def record(self, *, evidence, assessment):
            calls.append(("record", evidence.capture_id, assessment.gate_present))

    class Coordinator:
        async def coordinate(self, command):
            calls.append(("click", command.intent.source_capture_id, "web_age_gate_confirm"))
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id="capture-after"))

    class Reader:
        async def read(self, *, capture_id):
            calls.append(("read", capture_id))
            return _evidence(capture_id=capture_id, with_candidate=False)

    handler = WebAgeGateHandler(
        assessor=Assessor(), coordinator=Coordinator(), evidence_reader=Reader(),
        page_type_recorder=Recorder(),
    )
    if expected:
        with pytest.raises(expected):
            await handler.handle(entry, adult_self_attestation=_attestation() if declared else None)
        assert calls == ([("record", "capture-entry", True)] if confidence >= 0.7 else [])
    else:
        result = await handler.handle(entry, adult_self_attestation=_attestation())
        assert calls == [("record", "capture-entry", True),
                         ("click", "capture-entry", "web_age_gate_confirm"),
                         ("read", "capture-after")]
        assert result.pre_gate_capture_id == result.entry_capture_id == "capture-entry"
        assert result.post_gate_capture_id == result.after_capture_id == "capture-after"
        assert result.dismiss_action_id


@pytest.mark.asyncio
async def test_age_gate_record_failure_prevents_click_and_propagates():
    entry = _evidence(capture_id="capture-entry")

    class Assessor:
        async def assess(self, **kwargs):
            return WebAgeGateAssessment(gate_present=True, confidence=0.99,
                confirmation_candidate_id=entry.candidates[0].candidate_id,
                human_verification_present=False, reason="年龄门")

    class Recorder:
        async def record(self, **kwargs):
            raise OSError("database unavailable")

    class Coordinator:
        async def coordinate(self, command):
            raise AssertionError("写入失败不得点击")

    with pytest.raises(OSError, match="database unavailable"):
        await WebAgeGateHandler(assessor=Assessor(), coordinator=Coordinator(),
            evidence_reader=object(), page_type_recorder=Recorder()).handle(
                entry, adult_self_attestation=_attestation())


def _evidence(*, capture_id: str, with_candidate: bool = True) -> WebAgeGateEvidence:
    snapshot = {
        "elements": [
            {
                "selector": "#adult-confirm",
                "tag": "button",
                "role": "button",
                "text": "我已年满18岁并进入",
                "class_tokens": ["confirm"],
                "ancestor_class_tokens": ["age-gate"],
                "web_obstruction_layer": "#entry-age-gate",
                "web_obstruction_layer_kind": "age_gate",
                "web_obstruction_layer_structural": True,
                "web_obstruction_close_semantics": True,
                "within_web_navigation": False,
                "rect": {"x": 760, "y": 600, "width": 400, "height": 60},
                "interactive": True,
                "visible": True,
                "effective_z_index": 900,
                "hit_test": {"center_hits_target": True},
            }
        ]
    }
    return WebAgeGateEvidence(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id=capture_id,
        screenshot_artifact_id=f"{capture_id}:screenshot",
        dom_artifact_id=f"{capture_id}:dom",
        candidates=(
            build_web_obstruction_candidates(
                capture_id=capture_id,
                ui_snapshot=snapshot,
            )
            if with_candidate
            else ()
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )


def _attestation() -> AdultSelfAttestation:
    return AdultSelfAttestation(
        declared=True,
        statement_version="adult-v1",
        declared_by="operator-1",
        declared_at=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "text,class_tokens",
    (
        ("我已满18岁，立即支付并进入", ["age-gate", "payment"]),
        ("我已满18岁，下载客户端继续", ["age-gate", "download"]),
        ("注册并确认已成年", ["age-gate", "register-form"]),
        ("18+", ["age-gate"]),
    ),
)
def test_age_gate_candidate_builder_rejects_unsafe_or_ambiguous_controls(
    text: str,
    class_tokens: list[str],
) -> None:
    snapshot = {
        "elements": [
            {
                "selector": "#unsafe-age-action",
                "tag": "button",
                "role": "button",
                "text": text,
                "class_tokens": class_tokens,
                "ancestor_class_tokens": ["age-gate"],
                "web_obstruction_layer": "#entry-age-gate",
                "web_obstruction_layer_kind": "age_gate",
                "web_obstruction_layer_structural": True,
                "web_obstruction_close_semantics": True,
                "within_web_navigation": False,
                "rect": {"x": 100, "y": 100, "width": 300, "height": 50},
                "interactive": True,
                "visible": True,
                "effective_z_index": 900,
                "hit_test": {"center_hits_target": True},
            }
        ]
    }

    assert build_web_obstruction_candidates(
        capture_id="capture-unsafe",
        ui_snapshot=snapshot,
    ) == ()


@pytest.mark.asyncio
async def test_no_age_gate_allows_missing_adult_attestation_without_click() -> None:
    """普通站点没有年龄门禁时，不得因缺少成人声明阻断首屏。"""

    class Assessor:
        async def assess(self, **kwargs):
            del kwargs
            return WebAgeGateAssessment(
                gate_present=False,
                confirmation_candidate_id=None,
                confidence=0.99,
                reason="页面没有年龄门禁",
                human_verification_present=False,
            )

    class Coordinator:
        async def coordinate(self, command):
            raise AssertionError(f"无门禁时不得点击：{command!r}")

    result = await WebAgeGateHandler(
        assessor=Assessor(),
        coordinator=Coordinator(),
        evidence_reader=object(),
    ).handle(
        _evidence(capture_id="capture-entry", with_candidate=False),
        adult_self_attestation=None,
    )

    assert result.gate_present is False
    assert result.clicked_candidate_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("adult_self_attestation", [None, object()])
async def test_detected_age_gate_rejects_missing_or_invalid_attestation_without_click(
    adult_self_attestation: object,
) -> None:
    """只有检测到年龄门禁后才校验声明，缺失或无效声明必须失败关闭。"""

    entry = _evidence(capture_id="capture-entry")
    assessment_calls = 0

    class Assessor:
        async def assess(self, **kwargs):
            nonlocal assessment_calls
            del kwargs
            assessment_calls += 1
            return WebAgeGateAssessment(
                gate_present=True,
                confirmation_candidate_id=entry.candidates[0].candidate_id,
                confidence=0.99,
                reason="页面要求确认已成年",
                human_verification_present=False,
            )

    class Coordinator:
        async def coordinate(self, command):
            raise AssertionError(f"缺少有效声明时不得点击：{command!r}")

    with pytest.raises(WebAdultAttestationRequiredError, match="成年声明"):
        await WebAgeGateHandler(
            assessor=Assessor(),
            coordinator=Coordinator(),
            evidence_reader=object(),
        ).handle(
            entry,
            adult_self_attestation=adult_self_attestation,  # type: ignore[arg-type]
        )

    assert assessment_calls == 1


@pytest.mark.asyncio
async def test_age_gate_clicks_only_llm_selected_safe_candidate() -> None:
    entry = _evidence(capture_id="capture-entry")
    after = _evidence(capture_id="capture-after", with_candidate=False)
    selected_id = entry.candidates[0].candidate_id

    class Assessor:
        def __init__(self) -> None:
            self.results = [
                WebAgeGateAssessment(
                    gate_present=True,
                    confirmation_candidate_id=selected_id,
                    confidence=0.98,
                    reason="页面要求确认已满18岁",
                    human_verification_present=False,
                ),
                WebAgeGateAssessment(
                    gate_present=False,
                    confirmation_candidate_id=None,
                    confidence=0.99,
                    reason="成年门禁已经消失",
                    human_verification_present=False,
                ),
            ]

        async def assess(self, **kwargs):
            assert all("selector" not in item for item in kwargs["candidates"])
            return self.results.pop(0)

    class Coordinator:
        def __init__(self) -> None:
            self.intents = []

        async def coordinate(self, command):
            self.intents.append(command.intent)
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id="capture-after")
            )

    class EvidenceReader:
        async def read(self, *, capture_id):
            assert capture_id == "capture-after"
            return after

    coordinator = Coordinator()
    result = await WebAgeGateHandler(
        assessor=Assessor(),
        coordinator=coordinator,
        evidence_reader=EvidenceReader(),
    ).handle(entry, adult_self_attestation=_attestation())

    assert result.clicked_candidate_id == selected_id
    assert result.gate_disappeared is True
    assert len(coordinator.intents) == 1
    intent = coordinator.intents[0]
    assert intent.parameters["web_sealed_candidate_id"] == selected_id
    assert "selector" not in intent.parameters


@pytest.mark.asyncio
async def test_human_verification_is_never_clicked() -> None:
    entry = _evidence(capture_id="capture-entry")

    class Assessor:
        async def assess(self, **kwargs):
            del kwargs
            return WebAgeGateAssessment(
                gate_present=True,
                confirmation_candidate_id=None,
                confidence=0.99,
                reason="页面存在人机验证",
                human_verification_present=True,
            )

    class Coordinator:
        def __init__(self) -> None:
            self.intents = []

        async def coordinate(self, command):
            self.intents.append(command.intent)

    coordinator = Coordinator()
    handler = WebAgeGateHandler(
        assessor=Assessor(),
        coordinator=coordinator,
        evidence_reader=object(),
    )

    with pytest.raises(WebHumanVerificationBlockedError):
        await handler.handle(entry, adult_self_attestation=_attestation())

    assert coordinator.intents == []


@pytest.mark.asyncio
async def test_age_gate_remaining_after_click_requires_review() -> None:
    entry = _evidence(capture_id="capture-entry")
    selected_id = entry.candidates[0].candidate_id

    class Assessor:
        async def assess(self, **kwargs):
            del kwargs
            return WebAgeGateAssessment(
                gate_present=True,
                confirmation_candidate_id=selected_id,
                confidence=0.98,
                reason="门禁仍然存在",
                human_verification_present=False,
            )

    class Coordinator:
        async def coordinate(self, command):
            del command
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id="capture-after")
            )

    class EvidenceReader:
        async def read(self, *, capture_id):
            del capture_id
            return _evidence(capture_id="capture-after")

    with pytest.raises(WebAgeGateReviewRequiredError, match="未消失"):
        await WebAgeGateHandler(
            assessor=Assessor(),
            coordinator=Coordinator(),
            evidence_reader=EvidenceReader(),
        ).handle(entry, adult_self_attestation=_attestation())


@pytest.mark.asyncio
async def test_age_gate_auto_disappearing_before_click_continues_from_fresh_capture() -> None:
    entry = _evidence(capture_id="capture-entry")
    fresh = _evidence(capture_id="capture-fresh", with_candidate=False)
    selected_id = entry.candidates[0].candidate_id

    class Assessor:
        def __init__(self) -> None:
            self.results = [
                WebAgeGateAssessment(
                    gate_present=True,
                    confirmation_candidate_id=selected_id,
                    confidence=0.98,
                    reason="页面要求确认已满18岁",
                    human_verification_present=False,
                ),
                WebAgeGateAssessment(
                    gate_present=False,
                    confirmation_candidate_id=None,
                    confidence=0.99,
                    reason="动作前门禁已经自行消失",
                    human_verification_present=False,
                ),
            ]

        async def assess(self, **kwargs):
            del kwargs
            return self.results.pop(0)

    class Coordinator:
        async def coordinate(self, command):
            raise stale_web_candidate_error(command.intent)

    class EvidenceReader:
        async def read(self, *, capture_id):
            assert capture_id == "capture-fresh"
            return fresh

    result = await WebAgeGateHandler(
        assessor=Assessor(),
        coordinator=Coordinator(),
        evidence_reader=EvidenceReader(),
    ).handle(entry, adult_self_attestation=_attestation())

    assert result.gate_present is True
    assert result.clicked_candidate_id is None
    assert result.after_capture_id == "capture-fresh"
    assert result.gate_disappeared is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", STRICT_STALE_MUTATIONS)
async def test_age_gate_rejects_stale_error_without_complete_web_action_chain(
    mutation: str,
) -> None:
    entry = _evidence(capture_id="capture-entry")
    fresh = _evidence(capture_id="capture-fresh", with_candidate=False)
    selected_id = entry.candidates[0].candidate_id

    class Assessor:
        def __init__(self) -> None:
            self.results = [
                WebAgeGateAssessment(
                    gate_present=True,
                    confirmation_candidate_id=selected_id,
                    confidence=0.98,
                    reason="页面要求确认已满18岁",
                    human_verification_present=False,
                ),
                WebAgeGateAssessment(
                    gate_present=False,
                    confirmation_candidate_id=None,
                    confidence=0.99,
                    reason="动作前门禁已经自行消失",
                    human_verification_present=False,
                ),
            ]

        async def assess(self, **kwargs):
            del kwargs
            return self.results.pop(0)

    class Coordinator:
        async def coordinate(self, command):
            raise stale_web_candidate_error(command.intent, mutation=mutation)

    class EvidenceReader:
        async def read(self, *, capture_id):
            assert capture_id == "capture-fresh"
            return fresh

    with pytest.raises(CaptureCoordinationError):
        await WebAgeGateHandler(
            assessor=Assessor(),
            coordinator=Coordinator(),
            evidence_reader=EvidenceReader(),
        ).handle(entry, adult_self_attestation=_attestation())


@pytest.mark.asyncio
async def test_age_gate_rejects_fresh_capture_from_another_task() -> None:
    entry = _evidence(capture_id="capture-entry")
    fresh = replace(
        _evidence(capture_id="capture-fresh", with_candidate=False),
        task_id="other-task",
    )
    selected_id = entry.candidates[0].candidate_id

    class Assessor:
        def __init__(self) -> None:
            self.results = [
                WebAgeGateAssessment(
                    gate_present=True,
                    confirmation_candidate_id=selected_id,
                    confidence=0.98,
                    reason="页面要求确认已满18岁",
                    human_verification_present=False,
                ),
                WebAgeGateAssessment(
                    gate_present=False,
                    confirmation_candidate_id=None,
                    confidence=0.99,
                    reason="错误任务的页面无遮挡",
                    human_verification_present=False,
                ),
            ]

        async def assess(self, **kwargs):
            del kwargs
            return self.results.pop(0)

    class Coordinator:
        async def coordinate(self, command):
            raise stale_web_candidate_error(command.intent)

    class EvidenceReader:
        async def read(self, *, capture_id):
            assert capture_id == "capture-fresh"
            return fresh

    with pytest.raises(CaptureCoordinationError):
        await WebAgeGateHandler(
            assessor=Assessor(),
            coordinator=Coordinator(),
            evidence_reader=EvidenceReader(),
        ).handle(entry, adult_self_attestation=_attestation())
