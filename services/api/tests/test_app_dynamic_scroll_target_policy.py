"""动态业务页面整页滚动与目标风险复核回归测试。"""

from __future__ import annotations

from dataclasses import replace

import pytest

import services.api.tests.test_app_interaction_runtime as runtime_test
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionResultStatus,
    InteractionRiskLevel,
)


class _ContextRecordingDriver(runtime_test.RecordingDriver):
    def __init__(self) -> None:
        super().__init__()
        self.last_context = None

    async def execute_with_context(
        self,
        *,
        session,
        intent,
        action_execution_id,
        execution_context,
    ):
        self.last_context = execution_context
        return await self.execute(
            session=session,
            intent=intent,
            action_execution_id=action_execution_id,
        )


def _dynamic_scroll_request(
    candidate: runtime_test.AppVisualCandidate,
    *,
    page_type: str = "payment",
    parameters: dict[str, object] | None = None,
):
    purpose = f"动态采样当前业务页面下一屏；scope=nav:payment；page_type={page_type}"
    request = runtime_test._request(
        candidate,
        parameters={
            "app_dynamic_page_sampling": {
                "protocol_version": runtime_test.APP_DYNAMIC_PAGE_SAMPLING_VERSION,
                "business_page_scope": "nav:payment",
                "page_type": page_type,
            },
            "direction": "down",
            **(parameters or {}),
        },
    )
    return replace(request, intent=replace(request.intent, purpose=purpose))


def _dynamic_scroll_runtime(
    *,
    candidate: runtime_test.AppVisualCandidate | None = None,
    parameters: dict[str, object] | None = None,
    page_type: str = "payment",
    driver: runtime_test.RecordingDriver | None = None,
):
    visual_candidate = runtime_test._candidate()
    executor, driver, _ = runtime_test._executor(
        candidate=visual_candidate,
        analysis=runtime_test._analysis(
            visual_candidate,
            visual_page_type=page_type,
        ),
        driver=driver,
        command_validator=InteractionCommandValidator(),
    )
    dynamic_candidate = candidate or runtime_test._candidate(
        action_type=InteractionOperation.SCROLL,
        semantic_name="动态页面安全下滑",
        expected_state_hint=page_type,
        purpose=(
            f"动态采样当前业务页面下一屏；scope=nav:payment；page_type={page_type}"
        ),
    )
    return executor, driver, _dynamic_scroll_request(
        dynamic_candidate,
        page_type=page_type,
        parameters=parameters,
    )


async def test_payment_page_dynamic_scroll_reaches_driver_once() -> None:
    driver = _ContextRecordingDriver()
    executor, driver, request = _dynamic_scroll_runtime(driver=driver)
    request = replace(
        request,
        finalization_epoch=0,
        command_sequence=1,
        lease_owner="test-worker",
        lease_token="test-lease",
    )

    result = await executor.execute(request)

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent.action_type is InteractionOperation.SCROLL
    assert driver.last_intent.parameters["direction"] == "down"
    receipt = result.state_summary["app_command_broker_receipt"]
    assert receipt["result_summary"]["driver_invoked"] is True
    assert driver.last_context["resolved_candidate_id"] == request.intent.parameters[
        "candidate_id"
    ]


async def test_real_payment_click_is_blocked_before_driver() -> None:
    candidate = runtime_test._candidate(
        semantic_name="立即支付",
        purpose="完成订单支付",
        risk_level=InteractionRiskLevel.LOW,
    )
    executor, driver, _ = runtime_test._executor(
        candidate=candidate,
        analysis=runtime_test._analysis(candidate, visual_page_type="payment"),
        before_analysis=runtime_test._before_visual_analysis(
            candidate,
            visual_page_type="payment",
        ),
        command_validator=InteractionCommandValidator(),
    )

    result = await executor.execute(runtime_test._request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == "COMMAND_TARGET_RISK_UNDERRATED"
    assert result.state_summary["driver_invoked"] is False
    assert driver.execute_count == 0


async def test_dynamic_scroll_still_obeys_server_operation_policy() -> None:
    executor, driver, request = _dynamic_scroll_runtime()
    executor._command_validator = InteractionCommandValidator(
        policy=runtime_test.CommandPolicy(allowed_operations={}),
    )

    result = await executor.execute(request)

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == "COMMAND_OPERATION_NOT_ALLOWED"
    assert result.state_summary["driver_invoked"] is False
    assert driver.execute_count == 0


async def test_saved_payment_scroll_without_dynamic_contract_is_blocked_before_driver() -> None:
    candidate = runtime_test._candidate(
        action_type=InteractionOperation.SCROLL,
        semantic_name="支付说明下一屏",
        purpose="继续查看 payment 页面内容",
        expected_state_hint="payment",
        risk_level=InteractionRiskLevel.LOW,
    )
    executor, driver, _ = runtime_test._executor(
        candidate=candidate,
        analysis=runtime_test._analysis(candidate, visual_page_type="payment"),
        before_analysis=runtime_test._before_visual_analysis(
            candidate,
            visual_page_type="payment",
        ),
        command_validator=InteractionCommandValidator(),
    )

    result = await executor.execute(runtime_test._request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == "COMMAND_TARGET_RISK_UNDERRATED"
    assert result.state_summary["driver_invoked"] is False
    assert driver.execute_count == 0


@pytest.mark.parametrize(
    ("case", "candidate", "parameters", "page_type", "expected_error"),
    [
        (
            "identity",
            None,
            {"app_dynamic_page_sampling": {
                "protocol_version": runtime_test.APP_DYNAMIC_PAGE_SAMPLING_VERSION,
                "business_page_scope": "nav:payment",
                "page_type": "login",
            }},
            "payment",
            "VISUAL_CANDIDATE_NOT_FOUND",
        ),
        (
            "wrong_action",
            runtime_test._candidate(
                action_type=InteractionOperation.CLICK,
                semantic_name="动态页面安全下滑",
                expected_state_hint="payment",
                purpose="动态采样当前业务页面下一屏；scope=nav:payment；page_type=payment",
            ),
            None,
            "payment",
            "VISUAL_CANDIDATE_NOT_FOUND",
        ),
        (
            "bounds",
            runtime_test._candidate(
                action_type=InteractionOperation.SCROLL,
                semantic_name="动态页面安全下滑",
                expected_state_hint="payment",
                purpose="动态采样当前业务页面下一屏；scope=nav:payment；page_type=payment",
                normalized_bounds=runtime_test.NormalizedBounds(0.1, 0.1, 0.9, 0.2),
            ),
            None,
            "payment",
            "VISUAL_CANDIDATE_NOT_FOUND",
        ),
        (
            "coordinates",
            None,
            {
                "point": {"x": 12, "y": 34, "space": "device_px"},
                "resolved_target": {
                    "x": 12,
                    "y": 34,
                    "space": "capture_px",
                },
            },
            "payment",
            "COMMAND_TARGET_RISK_UNDERRATED",
        ),
        (
            "custom_swipe_path",
            None,
            {
                "from": {"x": 100, "y": 1200},
                "to": {"x": 100, "y": 300},
            },
            "payment",
            "COMMAND_TARGET_RISK_UNDERRATED",
        ),
        (
            "wrong_direction",
            None,
            {"direction": "left"},
            "payment",
            "COMMAND_TARGET_RISK_UNDERRATED",
        ),
    ],
)
async def test_forged_dynamic_scroll_contract_never_bypasses_payment_risk(
    case: str,
    candidate: runtime_test.AppVisualCandidate | None,
    parameters: dict[str, object] | None,
    page_type: str,
    expected_error: str,
) -> None:
    del case
    executor, driver, request = _dynamic_scroll_runtime(
        candidate=candidate,
        parameters=parameters,
        page_type=page_type,
    )

    try:
        result = await executor.execute(request)
    except runtime_test.AppInteractionRuntimeError as error:
        assert error.error_code == expected_error
    else:
        assert result.status is InteractionResultStatus.FAILED
        assert result.error_code == expected_error
    assert driver.execute_count == 0
