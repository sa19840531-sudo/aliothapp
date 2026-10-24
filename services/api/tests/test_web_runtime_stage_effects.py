"""真实阶段适配器与窄服务之间的恢复合同。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
from services.api.app.application.web_full_page_supplemental_capture import (
    WebFullPageSupplementalResult,
)
from services.api.app.application.web_run_stage_orchestrator import (
    WebStageEffectResult,
    WebStageFailure,
)
from services.api.app.application.web_runtime_stage_effects import WebRuntimeStageEffects
from services.api.app.domain.exploration.web_run_stage_checkpoint import (
    WebRunStage,
    WebStageCursor,
)

RUN = "00000000-0000-0000-0000-000000000006"
TASK = "00000000-0000-0000-0000-000000000001"
CAPTURE = "00000000-0000-0000-0000-000000000007"
ARTIFACT = "00000000-0000-0000-0000-000000000008"
ARTIFACT_2 = "00000000-0000-0000-0000-00000000000a"


class Evidence:
    def __init__(self):
        self.closed = False
        self.saved = {}
        self.epoch = 2
        self.assessment_recorded = True
        self.durable_assessment = None

    async def assert_active(self, cursor):
        if cursor.cursor_payload["finalization_epoch"] != self.epoch:
            raise RuntimeError("WEB_STAGE_SCOPE_INVALID")

    async def find_completed(self, cursor, *, operation=None):
        return self.saved.get((cursor.idempotency_key, operation))

    async def save_completed(self, cursor, outcome, *, operation=None):
        self.saved[cursor.idempotency_key, operation] = outcome

    async def has_durable_page_type_assessment(self, *, capture_id, finalization_epoch):
        assert capture_id == CAPTURE
        assert finalization_epoch == self.epoch
        return self.assessment_recorded

    async def read_durable_page_type_assessment(self, *, capture_id, finalization_epoch):
        assert capture_id == CAPTURE
        assert finalization_epoch == self.epoch
        return self.durable_assessment

    async def session_closed(self, *, session_id):
        assert session_id == f"web:{RUN}"
        return self.closed

    async def capture_reference(self, *, capture_id):
        return {"capture_id": capture_id, "current_url": "https://example.test/"}

    async def current_capture_reference(self):
        return await self.capture_reference(capture_id=CAPTURE)

    async def read_epoch(self):
        return self.epoch


def cursor(stage):
    return WebStageCursor.start(
        run_id=RUN,
        logical_page_key="home",
        stage=stage,
        cursor_payload={
            "session_id": f"web:{RUN}",
            "finalization_epoch": 2,
            "capture_id": CAPTURE,
            "current_url": "https://example.test/",
        },
    ).claim(worker_id="worker", now=datetime.now(UTC))


def command():
    return RunWebWorkflowCommand(
        task_id=TASK,
        agent_run_id=RUN,
        session_id=f"web:{RUN}",
        target_url="https://example.test/",
        session_owner="web-v2-workflow",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=3),
    )


async def test_full_page_adapter_recovers_persisted_artifact_without_browser_execution():
    class FullPage:
        async def read_completed(self, request):
            assert request.source_capture_id == CAPTURE
            return WebFullPageSupplementalResult(
                status="captured",
                source_capture_id=CAPTURE,
                supplemental_capture_id="00000000-0000-0000-0000-000000000009",
                screenshot_artifact_id=ARTIFACT,
                screenshot_artifact_ids=(ARTIFACT, ARTIFACT_2),
                manifest_artifact_id="00000000-0000-0000-0000-00000000000b",
                reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
            )

        async def capture(self, _request):
            raise AssertionError("不得重复截图")

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(pre_gate_supplemental_capture=FullPage()),
        evidence=Evidence(),
        command=command(),
    )
    result = await effects.find_completed(cursor(WebRunStage.FULL_PAGE_CAPTURE))
    assert result.artifact_ids == (ARTIFACT, ARTIFACT_2)
    assert result.cursor_payload["capture_id"] == CAPTURE


async def test_full_page_refreshes_authoritative_baseline_after_supplemental_capture():
    refreshed_capture = "00000000-0000-0000-0000-000000000009"

    class FullPage:
        async def read_completed(self, _request):
            return None

        async def capture(self, request):
            return WebFullPageSupplementalResult(
                status="captured",
                source_capture_id=request.source_capture_id,
                supplemental_capture_id="00000000-0000-0000-0000-00000000000b",
                screenshot_artifact_id=ARTIFACT,
                reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
            )

    class Readiness:
        async def refresh_ready_baseline(self, *, source_capture_id, page_role):
            assert (source_capture_id, page_role) == (CAPTURE, "home")
            return refreshed_capture

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(
            pre_gate_supplemental_capture=FullPage(),
            page_readiness=Readiness(),
        ),
        evidence=Evidence(),
        command=command(),
    )

    result = await effects.execute(cursor(WebRunStage.FULL_PAGE_CAPTURE))

    assert result.artifact_ids == (ARTIFACT,)
    assert result.cursor_payload == {
        "capture_id": refreshed_capture,
        "current_url": "https://example.test/",
    }


async def test_completed_supplemental_does_not_skip_missing_baseline_refresh():
    class FullPage:
        async def read_completed(self, request):
            return WebFullPageSupplementalResult(
                status="captured",
                source_capture_id=request.source_capture_id,
                supplemental_capture_id="00000000-0000-0000-0000-00000000000b",
                screenshot_artifact_id=ARTIFACT,
                reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
            )

    class Readiness:
        async def refresh_ready_baseline(self, **_kwargs):
            raise AssertionError("find_completed 不得提前执行刷新")

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(
            pre_gate_supplemental_capture=FullPage(),
            page_readiness=Readiness(),
        ),
        evidence=Evidence(),
        command=command(),
    )

    assert await effects.find_completed(cursor(WebRunStage.FULL_PAGE_CAPTURE)) is None


async def test_classification_stage_does_not_reenter_full_page_supplementing_reader():
    classified = []

    class Ready:
        async def read(self, *, capture_id, page_role):
            assert (capture_id, page_role) == (CAPTURE, "home")
            return SimpleNamespace(capture_id=CAPTURE)

    class Classifier:
        async def classify(self, capture):
            classified.append(capture.capture_id)
            return SimpleNamespace(page_type="content_detail")

    runtime = SimpleNamespace(stage_ready_capture_reader=Ready(), page_type_classifier=Classifier())
    evidence = Evidence()
    effects = WebRuntimeStageEffects(runtime=runtime, evidence=evidence, command=command())
    result = await effects.execute(cursor(WebRunStage.CLASSIFICATION))
    assert classified == [CAPTURE]
    assert result.cursor_payload["capture_id"] == CAPTURE
    assert len(evidence.saved) == 2


async def test_classification_completed_marker_without_assessment_is_reclassified():
    current = cursor(WebRunStage.CLASSIFICATION)
    evidence = Evidence()
    evidence.assessment_recorded = False
    recorded = WebStageEffectResult(cursor_payload={"capture_id": CAPTURE})
    evidence.saved[current.idempotency_key, None] = recorded
    evidence.saved[current.idempotency_key, "classification"] = recorded
    calls = []

    class Ready:
        async def read(self, *, capture_id, page_role):
            assert (capture_id, page_role) == (CAPTURE, "home")
            return SimpleNamespace(capture_id=CAPTURE)

    class Classifier:
        async def classify(self, capture):
            calls.append(capture.capture_id)
            evidence.assessment_recorded = True
            return SimpleNamespace(page_type="home")

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(
            stage_ready_capture_reader=Ready(), page_type_classifier=Classifier()
        ),
        evidence=evidence,
        command=command(),
    )

    assert await effects.find_completed(current) is None
    await effects.execute(current)
    assert calls == [CAPTURE]


@pytest.mark.parametrize("page_type", ["home", "unknown", None])
async def test_classification_recovers_committed_assessment_without_rerunning_model(page_type):
    current = cursor(WebRunStage.CLASSIFICATION)
    evidence = Evidence()
    evidence.durable_assessment = {"page_type": page_type}

    class Ready:
        async def read(self, **_kwargs):
            raise AssertionError("已持久 Assessment 不应重新读取 current-only 快照")

    class Classifier:
        async def classify(self, _capture):
            raise AssertionError("已持久 Assessment 不应再次请求模型")

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(
            stage_ready_capture_reader=Ready(), page_type_classifier=Classifier()
        ),
        evidence=evidence,
        command=command(),
    )

    result = await effects.execute(current)
    assert result.cursor_payload["capture_id"] == CAPTURE
    assert (current.idempotency_key, "classification") in evidence.saved


async def test_classification_rejects_completed_marker_from_another_capture():
    current = cursor(WebRunStage.CLASSIFICATION)
    current = replace(
        current,
        cursor_payload={
            **current.cursor_payload,
            "capture_id": "00000000-0000-0000-0000-000000000009",
        },
    )
    evidence = Evidence()
    recorded = WebStageEffectResult(cursor_payload={"capture_id": CAPTURE})
    evidence.saved[current.idempotency_key, None] = recorded
    evidence.saved[current.idempotency_key, "classification"] = recorded

    class Ready:
        async def read(self, **_kwargs):
            raise AssertionError("不能读取另一个 Capture")

    class Classifier:
        async def classify(self, _capture):
            raise AssertionError("不能用另一帧的分类结果")

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(
            stage_ready_capture_reader=Ready(), page_type_classifier=Classifier()
        ),
        evidence=evidence,
        command=command(),
    )

    with pytest.raises(WebStageFailure) as lookup_error:
        await effects.find_completed(current)
    assert lookup_error.value.code == "WEB_CLASSIFICATION_CAPTURE_MISMATCH"
    with pytest.raises(WebStageFailure) as error:
        await effects.execute(current)
    assert error.value.code == "WEB_CLASSIFICATION_CAPTURE_MISMATCH"


async def test_classification_does_not_complete_when_assessment_was_not_persisted():
    current = cursor(WebRunStage.CLASSIFICATION)
    evidence = Evidence()
    evidence.assessment_recorded = False

    class Ready:
        async def read(self, *, capture_id, page_role):
            assert (capture_id, page_role) == (CAPTURE, "home")
            return SimpleNamespace(capture_id=CAPTURE)

    class Classifier:
        async def classify(self, _capture):
            return SimpleNamespace(page_type="unknown")

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(
            stage_ready_capture_reader=Ready(), page_type_classifier=Classifier()
        ),
        evidence=evidence,
        command=command(),
    )

    with pytest.raises(WebStageFailure) as error:
        await effects.execute(current)
    assert error.value.code == "WEB_PAGE_TYPE_ASSESSMENT_MISSING"
    assert error.value.transient is True
    assert (current.idempotency_key, "classification") not in evidence.saved


async def test_classification_missing_assessment_with_stale_ready_capture_has_explicit_gap():
    current = cursor(WebRunStage.CLASSIFICATION)
    evidence = Evidence()
    evidence.assessment_recorded = False
    evidence.saved[current.idempotency_key, "classification"] = WebStageEffectResult(
        cursor_payload={"capture_id": CAPTURE}
    )

    class Ready:
        async def read(self, *, capture_id, page_role):
            assert (capture_id, page_role) == (CAPTURE, "home")
            raise RuntimeError("WEB_CAPTURE_IDENTITY_MISMATCH")

    class Classifier:
        async def classify(self, _capture):
            raise AssertionError("旧帧不应重新分类")

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(
            stage_ready_capture_reader=Ready(), page_type_classifier=Classifier()
        ),
        evidence=evidence,
        command=command(),
    )

    with pytest.raises(WebStageFailure) as error:
        await effects.execute(current)
    assert error.value.code == "WEB_CLASSIFICATION_READY_CAPTURE_UNAVAILABLE"
    assert error.value.transient is False


async def test_classification_accepts_durable_unknown_needs_review_assessment():
    current = cursor(WebRunStage.CLASSIFICATION)
    evidence = Evidence()

    class Ready:
        async def read(self, *, capture_id, page_role):
            assert (capture_id, page_role) == (CAPTURE, "home")
            return SimpleNamespace(capture_id=CAPTURE)

    class Classifier:
        async def classify(self, _capture):
            return SimpleNamespace(page_type="unknown", needs_review=True)

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(
            stage_ready_capture_reader=Ready(), page_type_classifier=Classifier()
        ),
        evidence=evidence,
        command=command(),
    )

    await effects.execute(current)
    assert (current.idempotency_key, "classification") in evidence.saved


async def test_classification_keeps_ready_baseline_for_menu_discovery_after_primary_capture(
) -> None:
    enhanced_capture = "00000000-0000-0000-0000-000000000009"

    class Ready:
        async def read(self, *, capture_id, page_role):
            assert (capture_id, page_role) == (CAPTURE, "home")
            return SimpleNamespace(capture_id=CAPTURE, url="https://example.test/")

    class Classifier:
        async def classify(self, capture):
            assert capture.capture_id == CAPTURE
            return SimpleNamespace(page_type="home")

    class Primary:
        async def capture(self, **kwargs):
            assert kwargs["capture_id"] == CAPTURE
            return SimpleNamespace(status="captured", evidence_capture_id=enhanced_capture)

    runtime = SimpleNamespace(
        stage_ready_capture_reader=Ready(),
        page_type_classifier=Classifier(),
        primary_content_capture=Primary(),
    )
    evidence = Evidence()
    effects = WebRuntimeStageEffects(runtime=runtime, evidence=evidence, command=command())

    classification_cursor = cursor(WebRunStage.CLASSIFICATION)
    result = await effects.execute(classification_cursor)

    assert result.cursor_payload["capture_id"] == CAPTURE
    primary = await evidence.find_completed(
        classification_cursor,
        operation="primary_content",
    )
    assert primary.cursor_payload["capture_id"] == enhanced_capture


async def test_href_discovery_uses_classified_ready_baseline_after_primary_scroll() -> None:
    enhanced_capture = "00000000-0000-0000-0000-000000000009"
    discovered = []

    class CurrentEvidence(Evidence):
        async def current_capture_reference(self):
            return {
                "capture_id": enhanced_capture,
                "current_url": "https://example.test/",
            }

    class Frontier:
        async def discover(self, _command, *, source_capture_id):
            discovered.append(source_capture_id)

    class Ads:
        async def schedule(self, current, checkpoints):
            assert current.cursor_payload["capture_id"] == CAPTURE
            assert checkpoints is not None

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(),
        evidence=CurrentEvidence(),
        command=command(),
        frontier=Frontier(),
        checkpoints=SimpleNamespace(),
    )
    effects._discovery._ads = Ads()

    await effects.execute(cursor(WebRunStage.HREF_DISCOVERY))

    assert discovered == [CAPTURE]


async def test_classification_discovers_navigation_before_primary_scroll_and_href_reuses_marker():
    order = []

    class Ready:
        async def read(self, *, capture_id, page_role):
            assert (capture_id, page_role) == (CAPTURE, "home")
            return SimpleNamespace(capture_id=CAPTURE, url="https://example.test/")

    class Classifier:
        async def classify(self, _capture):
            order.append("classify")
            return SimpleNamespace(page_type="home")

    class Frontier:
        async def discover(self, _command, *, source_capture_id):
            assert source_capture_id == CAPTURE
            order.append("discover")

    class Primary:
        async def capture(self, **_kwargs):
            order.append("scroll")
            return SimpleNamespace(
                status="failed", reason_code="WEB_PRIMARY_CONTENT_TARGET_DRIFTED"
            )

    evidence = Evidence()
    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(
            stage_ready_capture_reader=Ready(),
            page_type_classifier=Classifier(),
            primary_content_capture=Primary(),
        ),
        evidence=evidence,
        command=command(),
        frontier=Frontier(),
        checkpoints=SimpleNamespace(),
    )

    await effects.execute(cursor(WebRunStage.CLASSIFICATION))
    await effects.execute(cursor(WebRunStage.HREF_DISCOVERY))

    assert order == ["classify", "discover", "scroll"]
    assert await evidence.find_completed(
        cursor(WebRunStage.CLASSIFICATION), operation="list_href_discovery"
    ) is not None


async def test_classification_schedules_ad_before_nonblocking_primary_failure() -> None:
    order = []

    class Ready:
        async def read(self, *, capture_id, page_role):
            assert (capture_id, page_role) == (CAPTURE, "home")
            return SimpleNamespace(capture_id=CAPTURE, url="https://example.test/")

    class Classifier:
        async def classify(self, _capture):
            order.append("classification")
            return SimpleNamespace(page_type="home")

    class Primary:
        async def capture(self, **_kwargs):
            order.append("primary")
            return SimpleNamespace(
                status="failed",
                evidence_capture_id=None,
                reason_code="WEB_PRIMARY_CONTENT_TARGET_DRIFTED",
            )

    class Ads:
        async def schedule(self, current, checkpoints):
            assert current.cursor_payload["capture_id"] == CAPTURE
            assert checkpoints is not None
            order.append("ad")

    runtime = SimpleNamespace(
        stage_ready_capture_reader=Ready(),
        page_type_classifier=Classifier(),
        primary_content_capture=Primary(),
    )
    effects = WebRuntimeStageEffects(
        runtime=runtime,
        evidence=Evidence(),
        command=command(),
        checkpoints=SimpleNamespace(),
    )
    effects._ads = Ads()

    result = await effects.execute(cursor(WebRunStage.CLASSIFICATION))

    assert result.cursor_payload["capture_id"] == CAPTURE
    assert order == ["classification", "ad", "primary"]


async def test_guide_branch_receives_ready_snapshot_before_primary_scroll() -> None:
    order = []

    class Ready:
        async def read(self, *, capture_id, page_role):
            assert (capture_id, page_role) == (CAPTURE, "home")
            return SimpleNamespace(capture_id=CAPTURE, url="https://example.test/")

    class Classifier:
        async def classify(self, _capture):
            order.append("classification")
            return SimpleNamespace(page_type="home")

    class Snapshot:
        async def read_snapshot(self, *, capture_id):
            assert capture_id == CAPTURE
            return {"elements": [{"text": "APP 下载", "href": "/app"}]}

        async def read_url(self, *, capture_id):
            assert capture_id == CAPTURE
            return "https://example.test/"

    class Guide:
        async def schedule(self, *, source_capture_id, source_url, snapshot):
            assert source_capture_id == CAPTURE
            assert source_url == "https://example.test/"
            assert snapshot["elements"][0]["text"] == "APP 下载"
            order.append("guide")

    class Primary:
        async def capture(self, **_kwargs):
            order.append("scroll")
            return SimpleNamespace(
                status="failed", reason_code="WEB_PRIMARY_CONTENT_TARGET_DRIFTED",
            )

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(
            stage_ready_capture_reader=Ready(), page_type_classifier=Classifier(),
            ad_snapshot_reader=Snapshot(), web_guide_dispatcher=Guide(),
            primary_content_capture=Primary(),
        ),
        evidence=Evidence(), command=command(),
    )
    await effects.execute(cursor(WebRunStage.CLASSIFICATION))
    assert order == ["classification", "guide", "scroll"]


async def test_guide_branch_failure_does_not_fail_classification() -> None:
    class Ready:
        async def read(self, **_kwargs):
            return SimpleNamespace(capture_id=CAPTURE)

    class Classifier:
        async def classify(self, _capture):
            return SimpleNamespace(page_type="home")

    class Snapshot:
        async def read_snapshot(self, **_kwargs):
            raise RuntimeError("optional guide evidence unavailable")

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(
            stage_ready_capture_reader=Ready(), page_type_classifier=Classifier(),
            ad_snapshot_reader=Snapshot(), web_guide_dispatcher=SimpleNamespace(),
        ),
        evidence=Evidence(), command=command(),
    )
    result = await effects.execute(cursor(WebRunStage.CLASSIFICATION))
    assert result.cursor_payload["capture_id"] == CAPTURE


@pytest.mark.parametrize("gapped", [False, True])
async def test_classification_receives_completed_full_page_artifact_without_recapture(gapped):
    from dataclasses import dataclass

    from services.api.app.domain.exploration.web_run_stage_checkpoint import WebStageGap

    @dataclass
    class Ready:
        capture_id: str = CAPTURE
        supplemental_screenshot_artifact_ids: tuple[str, ...] = ()
        supplemental_evidence_status: str = "not_attempted"
        supplemental_evidence_reason_code: str | None = None

    class Reader:
        async def read(self, **kwargs):
            return Ready()

    class Classifier:
        async def classify(self, ready):
            assert ready.supplemental_screenshot_artifact_ids == (() if gapped else (ARTIFACT,))
            assert ready.supplemental_evidence_status == ("failed" if gapped else "captured")
            assert ready.supplemental_evidence_reason_code == (
                "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT" if gapped else None
            )

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(
            stage_ready_capture_reader=Reader(), page_type_classifier=Classifier()
        ),
        evidence=Evidence(),
        command=command(),
    )
    current = cursor(WebRunStage.CLASSIFICATION)
    if gapped:
        current = replace(
            current,
            gaps=(
                WebStageGap(
                    stage=WebRunStage.FULL_PAGE_CAPTURE,
                    stage_generation=0,
                    code="WEB_FULL_PAGE_SCREENSHOT_TIMEOUT",
                    error_code="WEB_FULL_PAGE_SCREENSHOT_TIMEOUT",
                    attempt_count=2,
                ),
            ),
        )
    else:
        current = replace(current, artifact_ids=(ARTIFACT,))
    await effects.execute(current)


async def test_session_close_reuses_durable_closed_session_without_reexploration():
    evidence = Evidence()
    evidence.closed = True
    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(), evidence=evidence, command=command()
    )
    result = await effects.find_completed(cursor(WebRunStage.SESSION_CLOSE))
    assert result is not None


async def test_session_close_deferred_replay_keeps_handoff_without_closing_browser(caplog):
    caplog.set_level("INFO", logger="services.api.app.application.web_runtime_stage_effects")
    from services.api.app.application.web_run_stage_orchestrator import WebStageEffectResult

    evidence = Evidence()
    current = cursor(WebRunStage.SESSION_CLOSE)
    fresh_effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(defer_session_close_to_reconciliation=True),
        evidence=evidence,
        command=command(),
    )
    assert await fresh_effects.find_completed(current) is None

    await evidence.save_completed(current, WebStageEffectResult())
    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(defer_session_close_to_reconciliation=True),
        evidence=evidence,
        command=command(),
    )

    result = await effects.find_completed(current)

    assert result is not None
    assert any(
        getattr(record, "session_close_status", None)
        == "deferred_to_formal_reconciliation"
        for record in caplog.records
    )


async def test_effect_lookup_rejects_stale_epoch_before_reading_artifacts():
    evidence = Evidence()
    evidence.epoch = 3
    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(), evidence=evidence, command=command()
    )
    with pytest.raises(RuntimeError, match="WEB_STAGE_SCOPE_INVALID"):
        await effects.find_completed(cursor(WebRunStage.FULL_PAGE_CAPTURE))


async def test_full_page_failure_is_typed_for_checkpoint_retry_without_advancing_capture():
    class FullPage:
        async def capture(self, request):
            return WebFullPageSupplementalResult(
                status="failed",
                source_capture_id=request.source_capture_id,
                supplemental_capture_id=None,
                screenshot_artifact_id=None,
                reason_code="WEB_FULL_PAGE_SCREENSHOT_TIMEOUT",
            )

    evidence = Evidence()
    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(pre_gate_supplemental_capture=FullPage()),
        evidence=evidence,
        command=command(),
    )
    with pytest.raises(WebStageFailure) as raised:
        await effects.execute(cursor(WebRunStage.FULL_PAGE_CAPTURE))
    assert raised.value.code == "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT"
    assert raised.value.transient
    assert evidence.saved == {}


async def test_bootstrap_stage_only_navigates_and_saves_capture_reference():
    actions = []

    class Runtime:
        async def start(self, request):
            actions.append(request.target_url)
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id=CAPTURE))

        async def prepare_home(self, *_args, **_kwargs):
            raise AssertionError("单阶段不得执行整页准备")

    effects = WebRuntimeStageEffects(runtime=Runtime(), evidence=Evidence(), command=command())
    result = await effects.execute(cursor(WebRunStage.BOOTSTRAP_NAVIGATION))
    assert actions == ["https://example.test/"]
    assert result.cursor_payload["capture_id"] == CAPTURE


async def test_close_stage_saves_durable_evidence_and_is_recovered_after_interruption():
    evidence = Evidence()
    closed = []

    class Runtime:
        async def close(self, *, task_id, session_id):
            closed.append(session_id)
            evidence.closed = True

    effects = WebRuntimeStageEffects(runtime=Runtime(), evidence=evidence, command=command())
    current = cursor(WebRunStage.SESSION_CLOSE)
    await effects.execute(current)
    evidence.saved.clear()
    assert await effects.find_completed(current) is not None
    assert closed == [f"web:{RUN}"]


@pytest.mark.parametrize(
    "stage", [WebRunStage.OBSTRUCTION_CLEAR, WebRunStage.READINESS, WebRunStage.PRIMARY_CAPTURE]
)
async def test_home_preparation_is_split_into_individual_stage_operations(stage):
    actions = []

    class AgeReader:
        async def read(self, *, capture_id):
            return SimpleNamespace(capture_id=capture_id)

    class AgeGate:
        async def handle(self, evidence, *, adult_self_attestation):
            actions.append("obstruction_clear")
            return SimpleNamespace(after_capture_id=CAPTURE)

    class Readiness:
        async def prepare(self, *, source_capture_id, page_role):
            actions.append("readiness")
            return SimpleNamespace(
                ready_capture_id=CAPTURE,
                status="stable",
                dismissed_layers=("modal-a", "modal-b"),
                evidence_capture_ids=("probe-a", CAPTURE),
            )

    class ReadyReader:
        async def read(self, *, capture_id, page_role):
            actions.append("primary_capture")
            return SimpleNamespace(capture_id=CAPTURE)

    runtime = SimpleNamespace(
        age_evidence_reader=AgeReader(),
        age_gate_handler=AgeGate(),
        page_readiness=Readiness(),
        stage_ready_capture_reader=ReadyReader(),
    )
    effects = WebRuntimeStageEffects(runtime=runtime, evidence=Evidence(), command=command())
    result = await effects.execute(cursor(stage))
    assert actions == [stage.value]
    assert result.cursor_payload["capture_id"] == CAPTURE
    if stage is WebRunStage.READINESS:
        assert set(result.cursor_payload) == {
            "capture_id",
            "current_url",
            "parent_capture_id",
        }


async def test_home_login_runs_after_mainline_capture_in_independent_branch():
    calls = []

    class AgeReader:
        async def read(self, *, capture_id):
            return SimpleNamespace(capture_id=capture_id)

    class AgeGate:
        async def handle(self, evidence, *, adult_self_attestation):
            calls.append("age")
            return SimpleNamespace(after_capture_id=CAPTURE)

    class Readiness:
        async def prepare(self, *, source_capture_id, page_role):
            calls.append("ready")
            return SimpleNamespace(status="stable", ready_capture_id=ARTIFACT)

    class ReadyReader:
        async def read(self, *, capture_id, page_role):
            assert (capture_id, page_role) == (ARTIFACT, "home")
            calls.append("mainline")
            return SimpleNamespace(capture_id=ARTIFACT)

    class Login:
        async def run(self, **kwargs):
            calls.append("login")
            assert kwargs["source_capture_id"] == ARTIFACT
            assert kwargs["session_id"] == f"web:{RUN}"
            return SimpleNamespace(
                status="authenticated", reason_code="WEB_LOGIN_COOKIE_CONFIRMED",
                cross_origin_status="blocked",
                blocked_requests=(("https://mc.yandex.ru", "GET"),),
                account_status="recharge_opened",
                account_artifact_ids=("artifact-1",),
                account_storage_paths=("task/capture/artifact-1.png",),
                account_page_type="topup_page",
                account_page_url="https://example.test/deposit",
                account_page_visits=(
                    ("recharge_page", "https://example.test/wallet",
                     "wallet-shot", "wallet-dom"),
                    ("topup_page", "https://example.test/deposit",
                     "topup-shot", "topup-dom"),
                ),
                account_avatar_opened=True,
                account_avatar_dom_artifact_id="artifact-dom",
            )

    runtime = SimpleNamespace(
        age_evidence_reader=AgeReader(),
        age_gate_handler=AgeGate(),
        page_readiness=Readiness(),
        stage_ready_capture_reader=ReadyReader(),
        web_login_branch=Login(),
    )
    async def read_persisted_exploration(_command):
        return SimpleNamespace(page_timeline=(ARTIFACT,))
    runtime.read_persisted_exploration = read_persisted_exploration
    effects = WebRuntimeStageEffects(runtime=runtime, evidence=Evidence(), command=command())
    obstruction = await effects.execute(cursor(WebRunStage.OBSTRUCTION_CLEAR))
    assert calls == ["age"]
    assert obstruction.cursor_payload["capture_id"] == CAPTURE
    ready = await effects.execute(cursor(WebRunStage.READINESS))
    assert calls == ["age", "ready"]
    assert ready.cursor_payload["capture_id"] == ARTIFACT
    assert ready.cursor_payload["login_source_capture_id"] == ARTIFACT
    mainline = await effects.execute(replace(
        cursor(WebRunStage.PRIMARY_CAPTURE),
        cursor_payload={**cursor(WebRunStage.PRIMARY_CAPTURE).cursor_payload,
                        **ready.cursor_payload},
    ))
    assert mainline.cursor_payload["capture_id"] == ARTIFACT
    assert calls == ["age", "ready", "mainline"]
    final = await effects.execute(replace(
        cursor(WebRunStage.REPORT_FINALIZATION),
        cursor_payload={**cursor(WebRunStage.REPORT_FINALIZATION).cursor_payload,
                        **ready.cursor_payload},
    ))
    assert calls == ["age", "ready", "mainline", "login"]
    assert final.cursor_payload["login_branch_status"] == "authenticated"
    assert final.cursor_payload["login_cross_origin_status"] == "blocked"
    assert final.cursor_payload["login_blocked_requests"] == (
        ("https://mc.yandex.ru", "GET"),
    )
    assert final.cursor_payload["login_account_status"] == "recharge_opened"
    assert final.cursor_payload["login_account_artifact_ids"] == ("artifact-1",)
    assert final.cursor_payload["login_account_avatar_opened"] is True
    assert final.cursor_payload["login_account_avatar_dom_artifact_id"] == "artifact-dom"
    assert final.cursor_payload["login_account_page_type"] == "topup_page"
    assert final.cursor_payload["login_account_page_url"] == "https://example.test/deposit"
    assert final.cursor_payload["login_account_page_visits"] == (
        ("recharge_page", "https://example.test/wallet", "wallet-shot", "wallet-dom"),
        ("topup_page", "https://example.test/deposit", "topup-shot", "topup-dom"),
    )


async def test_failed_independent_login_does_not_abort_main_exploration():
    class Readiness:
        async def prepare(self, *, source_capture_id, page_role):
            return SimpleNamespace(status="stable", ready_capture_id=ARTIFACT)

    class Login:
        async def run(self, **kwargs):
            return SimpleNamespace(status="incomplete", reason_code="WEB_LOGIN_UNVERIFIED")

    async def summary(_command):
        return SimpleNamespace(page_timeline=(ARTIFACT,))

    runtime = SimpleNamespace(
        page_readiness=Readiness(), web_login_branch=Login(),
        read_persisted_exploration=summary,
    )
    effects = WebRuntimeStageEffects(runtime=runtime, evidence=Evidence(), command=command())
    ready = await effects.execute(cursor(WebRunStage.READINESS))
    assert ready.cursor_payload["capture_id"] == ARTIFACT
    assert "login_branch_status" not in ready.cursor_payload
    final = await effects.execute(replace(
        cursor(WebRunStage.REPORT_FINALIZATION),
        cursor_payload={**cursor(WebRunStage.REPORT_FINALIZATION).cursor_payload,
                        **ready.cursor_payload},
    ))
    assert final.cursor_payload["login_branch_status"] == "incomplete"
    assert final.cursor_payload["login_branch_reason"] == "WEB_LOGIN_UNVERIFIED"


@pytest.mark.parametrize(
    ("status", "blocking_reasons", "expected_code", "transient"),
    [
        ("capture_failed", ("blank_document",), "WEB_READINESS_BLANK_DOCUMENT", True),
        ("timeout", ("readiness_deadline_exceeded",), "WEB_READINESS_TIMEOUT", True),
        ("popup_blocked", ("popup_blocked",), "WEB_READINESS_POPUP_BLOCKED", False),
        (
            "human_verification_blocked",
            ("human_verification_blocked",),
            "WEB_READINESS_HUMAN_VERIFICATION_BLOCKED",
            False,
        ),
    ],
)
async def test_readiness_stage_preserves_typed_root_failure(
    status, blocking_reasons, expected_code, transient
):
    class Readiness:
        async def prepare(self, *, source_capture_id, page_role):
            assert (source_capture_id, page_role) == (CAPTURE, "home")
            return SimpleNamespace(
                ready_capture_id=None,
                status=status,
                blocking_reasons=blocking_reasons,
            )

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(page_readiness=Readiness()),
        evidence=Evidence(),
        command=command(),
    )

    with pytest.raises(WebStageFailure) as raised:
        await effects.execute(cursor(WebRunStage.READINESS))

    assert raised.value.code == expected_code
    assert raised.value.transient is transient


@pytest.mark.parametrize(
    "stage",
    [
        WebRunStage.HREF_DISCOVERY,
        WebRunStage.CANDIDATE_NAVIGATION,
        WebRunStage.FRONTIER_DRAIN,
        WebRunStage.EVIDENCE_RECONCILIATION,
        WebRunStage.COVERAGE_SNAPSHOT,
        WebRunStage.REPORT_FINALIZATION,
    ],
)
async def test_remaining_business_stages_use_frontier_or_durable_summary_without_page_replay(stage):
    calls = []

    class Frontier:
        async def discover(self, command, *, source_capture_id):
            calls.append("discover")

        async def next_candidate(self, command, *, excluded_entry_ids):
            calls.append("next")
            return None

        async def reconcile(self, command, current):
            calls.append("reconcile")

    class Checkpoints:
        async def list_for_run(self, *, run_id):
            return (cursor(stage),)

    class Runtime:
        async def read_persisted_exploration(self, command):
            calls.append("summary")
            return SimpleNamespace(page_timeline=(CAPTURE,))

    effects = WebRuntimeStageEffects(
        runtime=Runtime(),
        evidence=Evidence(),
        command=command(),
        frontier=Frontier(),
        checkpoints=Checkpoints(),
    )
    await effects.execute(cursor(stage))
    expected = {
        WebRunStage.HREF_DISCOVERY: "discover",
        WebRunStage.CANDIDATE_NAVIGATION: None,
        WebRunStage.FRONTIER_DRAIN: "next",
        WebRunStage.EVIDENCE_RECONCILIATION: "reconcile",
        WebRunStage.COVERAGE_SNAPSHOT: "summary",
        WebRunStage.REPORT_FINALIZATION: "summary",
    }
    assert calls == ([expected[stage]] if expected[stage] else [])


async def test_menu_without_detail_schedules_next_only_after_ledger_reconciliation():
    calls = []
    menu = replace(
        cursor(WebRunStage.CANDIDATE_NAVIGATION),
        logical_page_key="discovery:menu",
        cursor_payload={
            **cursor(WebRunStage.CANDIDATE_NAVIGATION).cursor_payload,
            "menu_ledger_id": "discovery:menu",
        },
    )

    class Frontier:
        async def reconcile(self, command, current):
            calls.append("finalize")

        async def next_candidate(self, command, *, excluded_entry_ids):
            calls.append("next")
            return None

    class Checkpoints:
        async def list_for_run(self, *, run_id):
            return (menu,)

    class Primary:
        async def advance_detail(self, current, *, frontier):
            return None

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(),
        evidence=Evidence(),
        command=command(),
        frontier=Frontier(),
        checkpoints=Checkpoints(),
    )
    effects._primary = Primary()
    await effects.execute(menu)
    await effects.execute(replace(menu, current_stage=WebRunStage.FRONTIER_DRAIN))
    assert calls == []
    await effects.execute(replace(menu, current_stage=WebRunStage.EVIDENCE_RECONCILIATION))
    assert calls == ["finalize", "next"]


@pytest.mark.parametrize("detail_child_exists", [False, True])
async def test_menu_with_unsuccessful_detail_attempt_does_not_stall_next_menu(
    detail_child_exists,
):
    calls = []
    menu = replace(
        cursor(WebRunStage.EVIDENCE_RECONCILIATION),
        logical_page_key="discovery:menu",
        cursor_payload={
            **cursor(WebRunStage.EVIDENCE_RECONCILIATION).cursor_payload,
            "menu_ledger_id": "discovery:menu",
        },
    )
    evidence = Evidence()
    attempt = replace(menu, current_stage=WebRunStage.CANDIDATE_NAVIGATION)
    from services.api.app.application.web_run_stage_orchestrator import WebStageEffectResult

    await evidence.save_completed(
        attempt,
        WebStageEffectResult(cursor_payload={"capture_id": CAPTURE}),
        operation="detail",
    )

    class Frontier:
        async def reconcile(self, command, current):
            calls.append("finalize")

        async def next_candidate(self, command, *, excluded_entry_ids):
            calls.append("next")
            return None

    class Checkpoints:
        async def list_for_run(self, *, run_id):
            return (
                menu,
                *(
                    (replace(menu, logical_page_key="detail:discovery:menu"),)
                    if detail_child_exists
                    else ()
                ),
            )

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(),
        evidence=evidence,
        command=command(),
        frontier=Frontier(),
        checkpoints=Checkpoints(),
    )
    await effects.execute(menu)
    assert calls == (["finalize"] if detail_child_exists else ["finalize", "next"])


async def test_optional_auxiliary_failure_does_not_abort_scheduled_menu_navigation():
    calls = []

    class Frontier:
        async def next_candidate(self, command, *, excluded_entry_ids):
            calls.append("next")
            return "menu-1", SimpleNamespace(url="https://example.test/category")

    class Checkpoints:
        async def list_for_run(self, *, run_id):
            return (cursor(WebRunStage.FRONTIER_DRAIN),)

        async def create(self, child):
            calls.append((child.logical_page_key, child.cursor_payload["requested_url"]))

    class Auxiliary:
        async def explore_all(self, command, *, source_capture_id):
            calls.append(("auxiliary", source_capture_id))
            error = RuntimeError("辅助候选来源已漂移")
            error.error_code = "WEB_AUXILIARY_CANDIDATE_SOURCE_MISMATCH"
            raise error

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(auxiliary_navigation_explorer=Auxiliary()),
        evidence=Evidence(),
        command=command(),
        frontier=Frontier(),
        checkpoints=Checkpoints(),
    )

    with pytest.raises(WebStageFailure, match="WEB_AUXILIARY_NAVIGATION_FAILED"):
        await effects.execute(cursor(WebRunStage.FRONTIER_DRAIN))

    assert calls == [
        "next",
        ("menu-1", "https://example.test/category"),
        ("auxiliary", CAPTURE),
    ]


async def test_child_bootstrap_navigates_candidate_without_reopening_home_session():
    visited = []

    class Frontier:
        async def get_candidate(self, request, *, entry_id):
            return SimpleNamespace(url="https://example.test/healthy")

        async def navigate(self, request, *, candidate, source_capture_id):
            visited.append(candidate.url)
            return SimpleNamespace(final_capture_id=CAPTURE)

    class Runtime:
        async def start(self, _request):
            raise AssertionError("候选导航不得再次启动首页")

    effects = WebRuntimeStageEffects(
        runtime=Runtime(), evidence=Evidence(), command=command(), frontier=Frontier()
    )
    child = cursor(WebRunStage.BOOTSTRAP_NAVIGATION)
    child = replace(
        child,
        logical_page_key="candidate",
        cursor_payload={
            **child.cursor_payload,
            "menu_ledger_id": "candidate",
        },
    )
    result = await effects.execute(child)
    assert result.cursor_payload["capture_id"] == CAPTURE
    assert visited == ["https://example.test/healthy"]


async def test_child_closeout_never_closes_shared_business_session():
    class Runtime:
        async def close(self, **_kwargs):
            raise AssertionError("仍需保留父页面共享 Session")

    effects = WebRuntimeStageEffects(runtime=Runtime(), evidence=Evidence(), command=command())
    await effects.execute(replace(cursor(WebRunStage.SESSION_CLOSE), logical_page_key="child"))
