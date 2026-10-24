"""真实协调错误须按根阶段预算恢复，并保留已完成审计引用。"""

from types import SimpleNamespace

import pytest

from services.api.app.application.capture_coordination import (
    CaptureCoordinationError,
    CaptureCoordinationStage,
)
from services.api.app.application.web_run_stage_orchestrator import WebStageFailure
from services.api.app.application.web_runtime_stage_effects import WebRuntimeStageEffects
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
from services.api.tests.test_web_runtime_stage_effects import CAPTURE, Evidence, command, cursor
from services.api.tests.web_runtime_recovery_fixture import RecoveryWorld


@pytest.mark.parametrize("retryable", [False, True])
async def test_bootstrap_coordination_error_preserves_capture_reference_and_retryability(retryable):
    class Runtime:
        async def start(self, request):
            raise CaptureCoordinationError(
                stage=CaptureCoordinationStage.ACTION,
                error_code="WEB_GOTO_TLS_FAILED",
                message="failed",
                retryable=retryable,
                before_capture=SimpleNamespace(capture_id=CAPTURE),
            )

    effects = WebRuntimeStageEffects(runtime=Runtime(), evidence=Evidence(), command=command())
    with pytest.raises(WebStageFailure) as caught:
        await effects.execute(cursor(WebRunStage.BOOTSTRAP_NAVIGATION))
    assert caught.value.code == "WEB_GOTO_TLS_FAILED"
    assert caught.value.transient is retryable
    assert caught.value.detail["capture_id"] == CAPTURE


@pytest.mark.parametrize(
    "raw_code,stage,want",
    [
        ("WEB_GOTO_TLS_FAILED", CaptureCoordinationStage.ACTION, "WEB_GOTO_TLS_FAILED"),
        (
            "after_capture_failed",
            CaptureCoordinationStage.AFTER_CAPTURE,
            "WEB_BOOTSTRAP_AFTER_CAPTURE_FAILED",
        ),
        (
            "before_capture_failed",
            CaptureCoordinationStage.BEFORE_CAPTURE,
            "WEB_BOOTSTRAP_BEFORE_CAPTURE_FAILED",
        ),
        (
            "future_lowercase_failure",
            CaptureCoordinationStage.ACTION,
            "WEB_BOOTSTRAP_COORDINATION_FAILED",
        ),
    ],
)
async def test_root_tls_coordination_error_retries_three_times_then_closes_safely(
    monkeypatch, raw_code, stage, want
):
    world = RecoveryWorld(monkeypatch)

    async def start(request):
        world.actions.append("bootstrap")
        raise CaptureCoordinationError(
            stage=stage,
            error_code=raw_code,
            message="failed",
            retryable=True,
            cause_type="CaptureTransportError",
        )

    world.runtime.start = start

    class Auxiliary:
        async def explore_all(self, *args, **kwargs):
            raise AssertionError("根导航没有可信主帧，不得执行辅助导航")

    world.runtime.auxiliary_navigation_explorer = Auxiliary()
    result = await world.run()
    assert world.actions.count("bootstrap") == 3
    assert world.closed
    assert "WEB_NAVIGATION_RETRY_EXHAUSTED" in result.gap_codes
    assert not world.classifications
    assert world.rows["home"].gaps[0].error_code == want
    details = world.rows["home"].last_error_detail
    assert details["raw_error_code"] == raw_code
    assert details["coordination_stage"] == stage.value
    assert details["cause_type"] == "CaptureTransportError"
    assert details["retryable"] is True
