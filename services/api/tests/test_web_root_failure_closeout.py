"""七站在线发现的空白首跳和未知首页须走真实阶段结算。"""

from types import SimpleNamespace

import pytest

from services.api.app.application.capture_coordination import (
    CaptureCoordinationError,
    CaptureCoordinationStage,
)
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.tests.web_runtime_recovery_fixture import RecoveryWorld


@pytest.mark.parametrize("blank_url", ["about:blank", "", None])
async def test_blank_before_navigation_failure_reaches_terminal_without_fake_page(
    monkeypatch, blank_url,
):
    world = RecoveryWorld(monkeypatch)
    blank = world.capture("about:blank", "before")
    world.captures[blank]["url"] = blank_url

    async def start(request):
        world.actions.append("bootstrap")
        raise CaptureCoordinationError(
            stage=CaptureCoordinationStage.ACTION,
            error_code="SURFACE_DRIVER_FAILED",
            message="failed",
            retryable=False,
            before_capture=SimpleNamespace(capture_id=blank),
            cause_type="SurfaceDriverError",
        )

    world.runtime.start = start
    result = await world.run()
    assert world.closed and world.rows["home"].is_terminal
    assert "WEB_NAVIGATION_RETRY_EXHAUSTED" in result.gap_codes
    assert world.rows["home"].gaps[0].error_code == "WEB_BOOTSTRAP_COORDINATION_FAILED"
    assert world.rows["home"].last_error_detail["raw_error_code"] == "SURFACE_DRIVER_FAILED"
    assert "capture_id" not in world.rows["home"].cursor_payload
    assert "capture_id" not in world.rows["home"].last_error_detail
    assert not world.classifications and not world.discoveries


@pytest.mark.parametrize("restored_classification", [False, True])
async def test_unknown_home_closes_without_authorizing_menu_or_auxiliary(
    monkeypatch, restored_classification,
):
    world = RecoveryWorld(monkeypatch, with_candidates=True)

    async def classify(ready):
        world.classifications.append(ready.url)
        world.captures[ready.capture_id]["page_type"] = "unknown"
        return SimpleNamespace(page_type="unknown")

    class Auxiliary:
        async def stage_candidates(self, *args, **kwargs):
            raise AssertionError("未知首页不得授权辅助页导航")

    world.runtime.page_type_classifier.classify = classify
    world.runtime.auxiliary_navigation_explorer = Auxiliary()

    def bind_role(session_id, capture_id, page_role):
        if page_role != "home":
            raise RuntimeError("WEB_DISCOVERY_PAGE_ROLE_INVALID")

    world.runtime.navigation_candidate_source.bind_navigation_page_role = bind_role
    if restored_classification:
        from services.api.app.application.web_run_stage_orchestrator import WebStageEffectResult
        from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage

        await world.run(stop_at="classification")
        row = world.rows["home"].advance(WebRunStage.CLASSIFICATION)
        assert row.current_stage is WebRunStage.CLASSIFICATION
        capture_id = row.cursor_payload["capture_id"]
        world.captures[capture_id]["page_type"] = "unknown"
        world.completed[row.idempotency_key, "classification"] = WebStageEffectResult(
            cursor_payload={"capture_id": capture_id},
        )
    result = await world.run()
    assert world.closed and world.rows["home"].is_terminal
    assert "WEB_HOME_PAGE_TYPE_UNCLASSIFIED" in result.gap_codes
    assert not world.discoveries and set(world.rows) == {"home"}
    assert world.records["home"].status is WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED
