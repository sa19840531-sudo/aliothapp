"""就绪阶段换帧后重试与耗尽收口，禁止绕过当前帧身份校验。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.app.application.web_menu_exploration import WebMenuVisitResult
from services.api.app.application.web_run_stage_orchestrator import WebStageFailure
from services.api.app.application.web_runtime_stage_effects import WebRuntimeStageEffects
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
from services.api.tests.test_web_runtime_stage_effects import CAPTURE, Evidence, command, cursor
from services.api.tests.web_runtime_recovery_fixture import RecoveryWorld

LATEST = "00000000-0000-0000-0000-000000000091"


@pytest.mark.parametrize("status", ["loading", "artifact_skew"])
async def test_failed_readiness_carries_verified_current_reference_into_retry(status):
    class CurrentEvidence(Evidence):
        async def current_capture_reference(self):
            return {"capture_id": LATEST, "current_url": "https://example.test/"}

    class Readiness:
        async def prepare(self, **_kwargs):
            return SimpleNamespace(status=status, ready_capture_id=None)

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(page_readiness=Readiness()),
        evidence=CurrentEvidence(), command=command(),
    )
    current = cursor(WebRunStage.READINESS)
    with pytest.raises(WebStageFailure) as error:
        await effects.execute(current)
    assert error.value.detail["capture_id"] == LATEST
    retry = current.schedule_retry(
        error_code=error.value.code, detail=error.value.detail, now=current.claimed_at,
    )
    assert retry.cursor_payload["capture_id"] == LATEST
    assert retry.last_error_detail["capture_id"] == LATEST
    assert retry.cursor_payload["session_id"] == current.cursor_payload["session_id"]
    assert retry.cursor_payload["finalization_epoch"] == 2


@pytest.mark.parametrize("status", ["loading", "artifact_skew"])
@pytest.mark.parametrize("mode", ["home_recovers", "home_exhausted", "menu_exhausted"])
async def test_production_runner_readiness_new_frame_retry_and_close(monkeypatch, status, mode):
    world = RecoveryWorld(monkeypatch, with_candidates=mode == "menu_exhausted")
    attempts = {}

    class Readiness:
        async def prepare(self, *, source_capture_id, page_role):
            assert source_capture_id == world.current, "WEB_CAPTURE_IDENTITY_MISMATCH"
            url = world.captures[source_capture_id]["url"]
            attempts[url] = attempts.get(url, 0) + 1
            current_id = world.capture(url, f"readiness-{attempts[url]}")
            fail = (
                url.endswith("/broken") if mode == "menu_exhausted"
                else mode == "home_exhausted" or attempts[url] == 1
            )
            return SimpleNamespace(
                status=status if fail else "stable",
                ready_capture_id=None if fail else current_id,
                evidence_capture_ids=(source_capture_id, current_id),
            )

    class Navigator:
        async def visit(self, *, candidate, **_kwargs):
            world.actions.append(candidate.url)
            return WebMenuVisitResult(
                final_capture_id=world.capture(candidate.url, "probe"), page_type=None,
            )

    world.runtime.page_readiness = Readiness()
    world.navigator = Navigator()
    await world.run()
    assert world.closed
    assert all(row.is_terminal for row in world.rows.values())
    if mode == "home_recovers":
        assert attempts == {"https://example.test/": 2}
        assert world.classifications == ["https://example.test/"]
        assert not world.rows["home"].gap_codes
    elif mode == "home_exhausted":
        assert attempts == {"https://example.test/": 2}
        assert not world.classifications
        assert f"WEB_READINESS_{status.upper()}" in world.rows["home"].gap_codes
    else:
        assert attempts["https://example.test/broken"] == 2
        assert attempts["https://example.test/healthy"] == 1
        assert "https://example.test/healthy" in world.classifications
        assert world.actions.count("https://example.test/broken") == 1


def test_readiness_retry_reference_does_not_replace_identity_or_parent():
    current = cursor(WebRunStage.READINESS)
    current = replace(
        current, cursor_payload={**current.cursor_payload, "parent_capture_id": CAPTURE},
    )
    retry = current.schedule_retry(
        error_code="WEB_READINESS_LOADING", now=current.claimed_at,
        detail={"capture_id": LATEST, "current_url": "https://example.test/"},
    )
    assert retry.cursor_payload == {**current.cursor_payload, "capture_id": LATEST}


async def test_readiness_retry_does_not_bypass_current_reference_ownership_failure():
    from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
        WebStageCheckpointConflict,
    )

    class CurrentEvidence(Evidence):
        async def current_capture_reference(self):
            raise WebStageCheckpointConflict("当前帧不属于活动会话")

    class Readiness:
        async def prepare(self, **_kwargs):
            return SimpleNamespace(status="loading", ready_capture_id=None)

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(page_readiness=Readiness()),
        evidence=CurrentEvidence(), command=command(),
    )
    with pytest.raises(WebStageCheckpointConflict):
        await effects.execute(cursor(WebRunStage.READINESS))


@pytest.mark.parametrize("detail", [
    {"capture_id": LATEST},
    {"current_url": "https://example.test/"},
    {"capture_id": "invalid", "current_url": "https://example.test/"},
    {"capture_id": LATEST, "current_url": "https://example.test/", "session_id": "other"},
    {"capture_id": LATEST, "current_url": "https://example.test/", "finalization_epoch": 3},
])
def test_readiness_failure_rejects_incomplete_or_cross_scope_reference(detail):
    with pytest.raises(ValueError):
        cursor(WebRunStage.READINESS).schedule_retry(
            error_code="WEB_READINESS_LOADING", now=cursor(WebRunStage.READINESS).claimed_at,
            detail=detail,
        )


@pytest.mark.parametrize("transient", [False, True])
def test_readiness_gap_keeps_latest_reference_when_nontransient_or_exhausted(transient):
    current = replace(cursor(WebRunStage.READINESS), attempt_count=2)
    settled = current.schedule_retry(
        error_code="WEB_READINESS_LOADING", transient=transient, now=current.claimed_at,
        detail={"capture_id": LATEST, "current_url": "https://example.test/next"},
    )
    assert settled.status.value == "gapped"
    assert settled.cursor_payload["capture_id"] == LATEST
    assert settled.cursor_payload["current_url"] == "https://example.test/next"


def test_other_stage_does_not_rebind_cursor_from_failure_diagnostics():
    current = cursor(WebRunStage.BOOTSTRAP_NAVIGATION)
    settled = current.schedule_retry(
        error_code="WEB_GOTO_TLS_FAILED", now=current.claimed_at,
        detail={"capture_id": LATEST, "current_url": "https://example.test/next"},
    )
    assert settled.cursor_payload == current.cursor_payload
