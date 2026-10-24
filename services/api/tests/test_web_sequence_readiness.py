"""真实 readiness 返回出口通知已有失败材料，不新增采集动作。"""

import pytest

from services.api.app.application.web_page_readiness import (
    WebPageReadinessCoordinator,
    WebPageReadinessResult,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["timeout", "loading", "popup_blocked", "stable"])
async def test_readiness_final_result_notifies_failure_sequence_once(status):
    calls = []

    async def notify(**kwargs):
        calls.append(kwargs)

    coordinator = WebPageReadinessCoordinator(
        assessor=None, coordinator=None, evidence_reader=None, failure_sequence_notifier=notify
    )
    original = WebPageReadinessResult(
        status=status,
        ready_capture_id="after-2" if status == "stable" else None,
        dismissed_layers=(),
        evidence_capture_ids=("after-1", "after-2"),
    )

    async def prepared(**kwargs):
        return original

    coordinator._prepare = prepared
    result = await coordinator.prepare(source_capture_id="source-1", page_role="menu")
    assert result.status == original.status
    assert len(calls) == (0 if status == "stable" else 1)
    if calls:
        assert calls[0]["result"].evidence_capture_ids == ("after-1", "after-2")
        assert calls[0]["source_capture_id"] == "source-1"


@pytest.mark.asyncio
async def test_formal_notification_finalizes_only_verified_existing_sequence_captures():
    from services.api.app.application.web_page_inspection_notification import (
        WebPageInspectionNotification,
    )

    calls = []

    class Repository:
        async def capture_ids_for_failure(self, **kwargs):
            calls.append("authorize")
            return ("before-1", "after-1", "before-2", "after-2")

        async def register_failure(self, **kwargs):
            calls.append("register")
            assert kwargs["evidence_capture_ids"] == ("after-1", "after-2")
            return {"status": "published"}

    class Finalizer:
        async def finalize_capture(self, **kwargs):
            calls.append(kwargs["capture_id"])

    notification = WebPageInspectionNotification(
        reader=None,
        inventory=None,
        publication_writer=None,
        sequence_repository=Repository(),
        manifest_finalizer=Finalizer(),
    )
    result = WebPageReadinessResult(
        status="timeout",
        ready_capture_id=None,
        dismissed_layers=(),
        evidence_capture_ids=("after-1", "after-2"),
    )
    assert (
        await notification.notify_readiness_failure(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            source_capture_id="source-1",
            page_role="menu",
            result=result,
        )
    )["status"] == "published"
    assert calls == ["authorize", "before-1", "after-1", "before-2", "after-2", "register"]


@pytest.mark.asyncio
async def test_actual_prepare_preserves_dismiss_action_when_ready_outcome_moves_to_observe():
    from services.api.tests.test_web_page_readiness import (
        Coordinator,
        EvidenceReader,
        ScriptedAssessor,
        _completed,
        _evidence,
    )

    evidence = [
        _evidence("source-1", text="关闭广告"),
        _evidence("close-after", text=None),
        _evidence("capture-ready", text=None),
    ]
    action = Coordinator(["close-after", "capture-ready"])
    delivered = []

    async def notify(**kwargs):
        delivered.append(kwargs["result"])

    readiness = WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({item.screenshot_artifact_id: item for item in evidence}),
        coordinator=action,
        evidence_reader=EvidenceReader({item.capture_id: item for item in evidence}),
        sleeper=lambda _: _completed(),
        interaction_sequence_notifier=notify,
    )
    result = await readiness.prepare(source_capture_id="source-1", page_role="menu")
    assert result.is_ready
    assert result.interaction_outcomes[0].after_capture_id == "capture-ready"
    assert result.interaction_action_refs == (
        {
            "outcome_ordinal": 0,
            "intent_id": action.intents[0].intent_id,
            "requested_source_capture_id": "source-1",
            "action_result_capture_id": "close-after",
        },
    )
    assert delivered == [result]
    assert [intent.action_type.value for intent in action.intents] == ["click", "observe"]


@pytest.mark.asyncio
async def test_success_notification_passes_original_action_provenance_without_stable_publication():
    from services.api.app.application.web_interaction_precision import WebInteractionOutcome
    from services.api.app.application.web_page_inspection_notification import (
        WebPageInspectionNotification,
    )

    received = []

    class Repository:
        async def capture_ids_for_failure(self, **kwargs):
            return ("before-1", "after-1", "before-2", "after-2")

        async def register_interaction(self, **kwargs):
            received.append(kwargs)
            return {"status": "published"}

    class Finalizer:
        async def finalize_capture(self, **kwargs):
            pass

    refs = (
        {
            "outcome_ordinal": 0,
            "intent_id": "dismiss",
            "requested_source_capture_id": "source",
            "action_result_capture_id": "after-1",
        },
    )
    result = WebPageReadinessResult(
        status="stable",
        ready_capture_id="after-2",
        dismissed_layers=(),
        evidence_capture_ids=("after-1", "after-2"),
        interaction_action_refs=refs,
        interaction_outcomes=(
            WebInteractionOutcome(
                command_accepted=True,
                effect_observed=True,
                postcondition_satisfied=False,
                transition_arrived=False,
                after_capture_id="after-2",
                failure_reason="not_restored",
            ),
        ),
    )
    notification = WebPageInspectionNotification(
        reader=None,
        inventory=None,
        publication_writer=None,
        manifest_finalizer=Finalizer(),
        sequence_repository=Repository(),
    )
    assert (
        await notification.notify_readiness_interaction(
            task_id="task",
            agent_run_id="run",
            session_id="web:run",
            source_capture_id="source",
            page_role="menu",
            result=result,
        )
    )["status"] == "published"
    assert received[0]["interaction_action_refs"] == refs
    assert received[0]["interaction_outcomes"][0]["postcondition_satisfied"] is False
