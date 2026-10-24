"""原稳定页证据增强的行为刻画与阶段接入合同。"""

from types import SimpleNamespace

import pytest

from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
from services.api.tests.test_web_runtime_stage_effects import CAPTURE, Evidence, command, cursor


async def test_legacy_stable_page_effect_characterization():
    from services.api.tests.test_web_runtime_composition import (
        test_home_classification_uses_primary_supplement_and_ledgers_latest_evidence,
        test_main_web_exploration_lifecycle_attempts_auxiliary_on_current_terminal_page,
    )

    await test_home_classification_uses_primary_supplement_and_ledgers_latest_evidence("known")
    await test_main_web_exploration_lifecycle_attempts_auxiliary_on_current_terminal_page()


async def test_primary_stage_adapter_invokes_existing_primary_port_and_reuses_terminal():
    from services.api.app.application.web_runtime_stage_primary import WebRuntimeStagePrimary

    calls = []

    class Primary:
        async def capture(self, **kwargs):
            calls.append(kwargs["capture_id"])
            return SimpleNamespace(evidence_capture_id=CAPTURE, status="captured", reason_code=None)

    evidence = Evidence()
    runtime = SimpleNamespace(primary_content_capture=Primary())
    adapter = WebRuntimeStagePrimary(runtime=runtime, evidence=evidence, command=command())
    current = cursor(WebRunStage.PRIMARY_CAPTURE)
    ready = SimpleNamespace(capture_id=CAPTURE, url="https://example.test/")
    await adapter.capture_primary(current, ready=ready, page_type="home")
    await WebRuntimeStagePrimary(
        runtime=runtime, evidence=evidence, command=command()
    ).capture_primary(current, ready=ready, page_type="home")
    assert calls == [CAPTURE]


async def test_default_builder_executes_primary_and_auxiliary_then_resumes_without_repeating(
    monkeypatch,
):
    from services.api.tests.web_runtime_recovery_fixture import RecoveryWorld

    world = RecoveryWorld(monkeypatch)

    class Primary:
        async def capture(self, **kwargs):
            world.actions.append("primary_content")
            return SimpleNamespace(evidence_capture_id=kwargs["capture_id"], status="captured")

    class Classifier:
        async def classify(self, ready):
            world.actions.append("authoritative_classification")
            world.captures[ready.capture_id]["page_type"] = "home"
            return SimpleNamespace(page_type="home", raw_page_type="home")

    class Auxiliary:
        async def explore_all(self, request, *, source_capture_id):
            world.actions.append("auxiliary")
            return ()

    world.runtime.primary_content_capture = Primary()
    world.runtime.page_type_classifier = Classifier()
    world.runtime.auxiliary_navigation_explorer = Auxiliary()
    await world.run(stop_at="full_page_capture")
    assert world.actions.count("primary_content") == 0
    assert "authoritative_classification" not in world.actions
    await world.run(stop_at="href_discovery")
    assert world.actions.index("full_page") < world.actions.index("authoritative_classification")
    await world.run()
    assert world.actions.count("primary_content") == 1
    assert world.actions.count("authoritative_classification") == 1
    assert world.actions.count("auxiliary") == 1
    assert world.closed


@pytest.mark.parametrize("detail_primary_fails", [False, True])
async def test_default_builder_keeps_list_detail_and_detail_primary_evidence(
    monkeypatch, detail_primary_fails
):
    import asyncio

    from services.api.app.application.web_list_detail_navigation import WebListDetailResult
    from services.api.app.application.web_runtime_stage_primary import WebRuntimeStagePrimary
    from services.api.app.domain.exploration import WebMenuTerminalStatus
    from services.api.tests.web_runtime_recovery_fixture import RecoveryWorld

    world = RecoveryWorld(monkeypatch, with_candidates=True)
    discovery_urls = []
    original_source = world.runtime.navigation_candidate_source.navigation_candidates

    async def navigation_candidates(session_id):
        discovery_urls.append(world.captures[world.current]["url"])
        return await original_source(session_id)

    world.runtime.navigation_candidate_source.navigation_candidates = navigation_candidates

    class Primary:
        async def capture(self, **kwargs):
            world.actions.append("primary:" + kwargs["page_type"])
            if detail_primary_fails and kwargs["page_type"] == "content_detail":
                return SimpleNamespace(
                    evidence_capture_id=None,
                    status="failed",
                    reason_code="WEB_PRIMARY_CONTENT_CAPTURE_FAILED",
                )
            return SimpleNamespace(evidence_capture_id=kwargs["capture_id"], status="captured")

    class Classifier:
        async def classify(self, ready):
            page_type = (
                "home"
                if ready.url == "https://example.test/"
                else "content_detail"
                if ready.url.endswith("/detail")
                else "content_list"
            )
            world.captures[ready.capture_id]["page_type"] = page_type
            return SimpleNamespace(page_type=page_type, raw_page_type=page_type)

    class Details:
        async def capture_one(self, evidence, **kwargs):
            world.actions.append("detail")
            capture_id = world.capture("https://example.test/healthy/detail", "ready")
            world.captures[capture_id]["page_type"] = "content_detail"
            return WebListDetailResult(
                status=WebMenuTerminalStatus.DETAIL_CAPTURED,
                detail_capture_id=capture_id,
                page_type="content_detail",
            )

    class Source:
        async def read_list(self, *, capture_id):
            return SimpleNamespace(ready_capture_id=capture_id)

    class PlayerVisual:
        async def assess(self, **kwargs):
            world.actions.append("player_visual:" + kwargs["page_type"])
            assert kwargs["allow_scroll"] is not detail_primary_fails
            if detail_primary_fails:
                raise RuntimeError("播放器补判服务不可用")
            return SimpleNamespace(
                status="uncertain", reason_code="WEB_PLAYER_VISUAL_TARGET_UNCERTAIN",
                evidence_capture_id=kwargs["capture_id"],
            )

    world.runtime.primary_content_capture = Primary()
    world.runtime.player_visual_recheck = PlayerVisual()
    world.runtime.page_type_classifier = Classifier()
    world.runtime.stage_detail_navigator = Details()
    world.runtime.stage_detail_evidence_source = Source()
    original = WebRuntimeStagePrimary._capture_detail
    interrupted = False

    async def interrupt_after_terminal(self, *args, **kwargs):
        nonlocal interrupted
        result = await original(self, *args, **kwargs)
        if not interrupted:
            interrupted = True
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(WebRuntimeStagePrimary, "_capture_detail", interrupt_after_terminal)
    with pytest.raises(asyncio.CancelledError):
        await world.run()
    await world.run()
    assert world.actions.count("detail") == 1
    assert "primary:content_list" in world.actions
    assert "primary:content_detail" in world.actions
    assert world.actions.count("player_visual:content_detail") == 1
    assert world.closed
    assert discovery_urls == ["https://example.test/"]
    assert any(key.startswith("detail:") for key in world.rows)
    detail_markers = [
        marker for (_key, operation), marker in world.completed.items() if operation == "detail"
    ]
    assert any(
        marker.optional_primary_error_code == "WEB_PRIMARY_CONTENT_CAPTURE_FAILED"
        for marker in detail_markers
    ) is detail_primary_fails


async def test_auxiliary_gap_still_reconciles_report_and_session_close(monkeypatch):
    from services.api.tests.web_runtime_recovery_fixture import RecoveryWorld

    world = RecoveryWorld(monkeypatch)

    class Auxiliary:
        async def explore_all(self, request, **kwargs):
            raise RuntimeError("WEB_AUXILIARY_NAVIGATION_FAILED")

    world.runtime.auxiliary_navigation_explorer = Auxiliary()
    result = await world.run()
    assert world.closed
    assert "WEB_AUXILIARY_NAVIGATION_FAILED" in result.gap_codes


async def test_primary_enhancement_failure_preserves_classification_and_later_safe_stages(
    monkeypatch,
):
    from services.api.tests.web_runtime_recovery_fixture import RecoveryWorld

    world = RecoveryWorld(monkeypatch)

    class Classifier:
        async def classify(self, ready):
            world.classifications.append(ready.url)
            world.captures[ready.capture_id]["page_type"] = "home"
            return SimpleNamespace(page_type="home")

    class Primary:
        async def capture(self, **kwargs):
            return SimpleNamespace(
                status="failed",
                evidence_capture_id=None,
                reason_code="WEB_PRIMARY_CONTENT_CAPTURE_FAILED",
            )

    world.runtime.primary_content_capture = Primary()
    world.runtime.page_type_classifier = Classifier()
    result = await world.run()
    assert world.classifications == ["https://example.test/"]
    assert world.closed
    assert "WEB_PRIMARY_CONTENT_CAPTURE_FAILED" not in result.gap_codes
    assert any(
        marker.optional_primary_error_code == "WEB_PRIMARY_CONTENT_CAPTURE_FAILED"
        for (key, operation), marker in world.completed.items()
        if operation is None
    )
