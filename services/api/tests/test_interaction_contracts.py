"""Action Intent 与 Interaction Result 的领域边界测试。"""

from datetime import UTC, datetime

import pytest

from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionValidationError,
    SurfaceKind,
)


def _capture_profile() -> CaptureProfile:
    return CaptureProfile(
        profile_id="critical-action",
        before=CaptureDirective(
            role="before",
            artifact_types=("screenshot", "ui_snapshot"),
        ),
        after=CaptureDirective(
            role="primary",
            artifact_types=("screenshot", "ui_snapshot", "action_trace"),
        ),
    )


def _intent(**overrides) -> ActionIntent:
    values = {
        "task_id": "task-1",
        "session_id": "session-1",
        "intent_id": "intent-1",
        "idempotency_key": "task-1:branch-1:intent-1",
        "surface_kind": SurfaceKind.ANDROID,
        "source_capture_id": "capture-source",
        "action_type": InteractionOperation.TAP,
        "semantic_target": "登录按钮",
        "parameters": {"input_text": "demo"},
        "purpose": "进入登录页",
        "capture_profile": _capture_profile(),
        "deadline": datetime(2026, 8, 3, 18, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return ActionIntent(**values)


def test_action_intent_is_complete_immutable_and_versioned() -> None:
    intent = _intent()

    with pytest.raises(TypeError):
        intent.parameters["input_text"] = "changed"
    payload = intent.to_dict()
    assert payload["protocol_version"] == "action-intent.v1"
    assert payload["task_id"] == "task-1"
    assert payload["session_id"] == "session-1"
    assert payload["source_capture_id"] == "capture-source"
    assert payload["semantic_target"] == "登录按钮"
    assert payload["capture_profile"]["protocol_version"] == "capture-profile.v1"
    assert payload["deadline"] == "2026-08-03T18:00:00+00:00"
    assert "driver" not in repr(payload).casefold()
    assert "lease" not in repr(payload).casefold()


def test_targeted_action_requires_semantic_target() -> None:
    with pytest.raises(InteractionValidationError, match="semantic_target"):
        _intent(semantic_target=None)


def test_seek_media_requires_one_bounded_target() -> None:
    intent = _intent(
        surface_kind=SurfaceKind.WEB,
        action_type=InteractionOperation.SEEK_MEDIA,
        semantic_target="视频播放进度条",
        parameters={"target_fraction": 0.5},
    )

    assert intent.parameters["target_fraction"] == 0.5
    for parameters in (
        {},
        {"target_fraction": 0.5, "target_time_seconds": 30},
        {"target_fraction": 0.01},
        {"target_time_seconds": 0},
    ):
        with pytest.raises(InteractionValidationError, match="seek_media|target_"):
            _intent(
                surface_kind=SurfaceKind.WEB,
                action_type=InteractionOperation.SEEK_MEDIA,
                semantic_target="视频播放进度条",
                parameters=parameters,
            )


def test_ad_landing_capture_requires_trusted_click_semantics_and_depth() -> None:
    intent = _intent(
        surface_kind=SurfaceKind.WEB,
        action_type=InteractionOperation.CLICK,
        semantic_target="广告外链入口",
        purpose="采集广告落地页",
        parameters={
            "navigation_depth": 1,
            "capture_ad_landing_evidence": True,
            "ad_landing_capability": "ad_outbound",
        },
    )

    assert intent.parameters["capture_ad_landing_evidence"] is True
    for overrides in (
        {
            "semantic_target": "广告外链入口",
            "purpose": "采集广告落地页",
            "parameters": {
                "navigation_depth": 1,
                "capture_ad_landing_evidence": True,
            },
        },
        {
            "semantic_target": "广告外链入口",
            "purpose": "采集广告落地页",
            "parameters": {
                "capture_ad_landing_evidence": True,
                "ad_landing_capability": "ad_outbound",
            },
        },
        {
            "semantic_target": "广告外链入口",
            "purpose": "采集广告落地页",
            "parameters": {
                "navigation_depth": 1,
                "capture_ad_landing_evidence": "yes",
                "ad_landing_capability": "ad_outbound",
            },
        },
        {
            "action_type": InteractionOperation.INPUT,
            "semantic_target": "广告外链入口",
            "purpose": "采集广告落地页",
            "parameters": {
                "navigation_depth": 1,
                "capture_ad_landing_evidence": True,
                "ad_landing_capability": "ad_outbound",
            },
        },
    ):
        with pytest.raises(InteractionValidationError, match="广告|capture_ad"):
            _intent(surface_kind=SurfaceKind.WEB, **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"semantic_target": "#login"},
        {"semantic_target": "xpath=//button"},
        {"parameters": {"selector": "#login"}},
        {"parameters": {"nested": {"appium_locator": "accessibility=登录"}}},
    ],
)
def test_action_intent_rejects_platform_specific_locators(overrides) -> None:
    with pytest.raises(InteractionValidationError, match="语义目标|禁止字段"):
        _intent(**overrides)


def test_action_intent_requires_timezone_aware_deadline() -> None:
    with pytest.raises(InteractionValidationError, match="带时区"):
        _intent(deadline=datetime(2026, 8, 3, 18, 0))


def test_interaction_success_requires_committed_after_capture() -> None:
    pending = InteractionResult(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-1",
        action_execution_id="action-1",
        source_capture_id="capture-before",
        status=InteractionResultStatus.EVIDENCE_PENDING,
        state_summary={"control": "登录按钮"},
        state_diff={"url_changed": True},
    )

    completed = pending.complete_with_capture("capture-after")

    assert pending.status == InteractionResultStatus.EVIDENCE_PENDING
    assert completed.status == InteractionResultStatus.SUCCEEDED
    assert completed.result_capture_id == "capture-after"
    assert completed.to_dict()["protocol_version"] == "interaction-result.v1"


def test_failed_interaction_requires_stable_error_code() -> None:
    with pytest.raises(InteractionValidationError, match="error_code"):
        InteractionResult(
            task_id="task-1",
            session_id="session-1",
            intent_id="intent-1",
            action_execution_id="action-1",
            source_capture_id="capture-before",
            status=InteractionResultStatus.FAILED,
        )


def test_capture_artifact_types_must_be_nonempty_array() -> None:
    with pytest.raises(InteractionValidationError, match="数组"):
        CaptureDirective(role="before", artifact_types="screenshot")
