"""广告消费后的首个安全原因必须跨阶段收口保持一致。"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from services.api.app.application.web_ad_landing_evidence import WebAdLandingCaptureError
from services.api.app.application.web_run_stage_orchestrator import WebStageFailure
from services.api.app.application.web_runtime_stage_ads import WebRuntimeStageAds
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
from services.api.app.infrastructure.browser.capture_runtime import WebAdLandingCaptureAdapter
from services.api.app.infrastructure.browser.page_capture import BlockedPageCaptureActionError
from services.api.app.infrastructure.db.web_ad_outbound_claim_repository import (
    PostgresWebAdOutboundClaimRepository,
)
from services.api.tests.test_web_runtime_stage_ads import (
    HomeEvidence,
    _persisted_ad_artifacts,
    ad_cursor,
    command,
)

PHASE = "surface_phase_run:" + "a" * 64
DENIED = "WEB_AD_OUTBOUND_NONPUBLIC_ADDRESS"


class Gate:
    @asynccontextmanager
    async def hold(self, **kwargs):
        yield SimpleNamespace(command_sequence=1, finalization_epoch=0)


class Claims:
    def __init__(self, finish_error=None):
        self.finished = []
        self.finish_error = finish_error

    async def claim(self, **kwargs):
        return PHASE

    async def finish(self, **kwargs):
        self.finished.append(kwargs)
        if self.finish_error is not None:
            raise self.finish_error


async def capture_failure(error, claims):
    class Browser:
        async def capture_with_timeout(self, request, **kwargs):
            raise error

    return await WebAdLandingCaptureAdapter(
        browser=Browser(), persistence=object(), session_command_gate=Gate(),
        outbound_claims=claims,
    ).capture(SimpleNamespace(
        task_id="task", session_id="session", source_capture_id="capture",
        source_url="https://source.example/", destination_url="https://ads.example/",
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_error", [None, RuntimeError("secret cleanup text")])
async def test_specific_browser_denial_survives_phase_finish(finish_error):
    claims = Claims(finish_error)
    with pytest.raises(WebAdLandingCaptureError) as caught:
        await capture_failure(
            BlockedPageCaptureActionError(
                "https://secret.invalid/?token=secret", error_code=DENIED,
            ),
            claims,
        )
    assert caught.value.reason_code == DENIED
    assert caught.value.detail == {
        "cause_type": "BlockedPageCaptureActionError", "raw_error_code": DENIED,
        "surface_phase_run_id": PHASE,
    }
    assert len(claims.finished) == 1
    assert claims.finished[0]["reason_code"] == DENIED
    assert claims.finished[0]["detail"] == caught.value.detail
    assert "secret" not in json.dumps(claims.finished)


@pytest.mark.asyncio
async def test_cancel_is_not_converted_or_replaced_by_finish_failure():
    claims = Claims(RuntimeError("cleanup"))
    cancellation = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as caught:
        await capture_failure(cancellation, claims)
    assert caught.value is cancellation
    assert claims.finished[0]["reason_code"] == "WEB_AD_CAPTURE_CANCELLED"


@pytest.mark.asyncio
async def test_unknown_failure_keeps_only_safe_type_and_phase_reference():
    claims = Claims()
    with pytest.raises(WebAdLandingCaptureError) as caught:
        await capture_failure(RuntimeError("https://secret.invalid/?token=secret"), claims)
    assert caught.value.reason_code == "WEB_AD_PAGE_OPEN_FAILED"
    assert caught.value.detail == {"cause_type": "RuntimeError", "surface_phase_run_id": PHASE}
    assert "secret" not in json.dumps(claims.finished)


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_id", [None, "persisted-before-error"])
async def test_failed_phase_restores_detail_through_closeout_without_capture(capture_id):
    detail = {"cause_type": "BlockedPageCaptureActionError", "raw_error_code": DENIED}

    class Connection:
        async def fetchrow(self, query, *args):
            return {
                "capture_id": capture_id, "page_type": "ad_page", "terminal_reason_code": DENIED,
                "phase_run_id": PHASE, "failure_detail": json.dumps(detail),
                "url": "https://ads.example/", "artifacts": _persisted_ad_artifacts(),
                "action_log": [{"action": "direct_ad_outbound", "navigation": {
                    "new_page_count": 1, "closed_after_observation_count": 1,
                }, "ad_navigation_evidence": {"status": "observed", "landing": {}}}],
            }

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    repository = PostgresWebAdOutboundClaimRepository(pool=Pool())

    class Port:
        async def read_terminal(self, request):
            return await repository.read_terminal(command=request)

        async def capture(self, request):
            pytest.fail("已消费广告不得重新导航")

    effects = WebRuntimeStageAds(
        runtime=SimpleNamespace(stage_ad_capture_port=Port()), command=command(),
        evidence=HomeEvidence(),
    )
    with pytest.raises(WebStageFailure) as caught:
        await effects.execute(ad_cursor(WebRunStage.AD_CLOSEOUT))
    assert caught.value.code == DENIED
    assert caught.value.detail == {**detail, "surface_phase_run_id": PHASE}
    assert caught.value.transient is False
    checkpoint = ad_cursor(WebRunStage.AD_CLOSEOUT).record_gap_and_advance(
        error_code=caught.value.code, detail=caught.value.detail,
    )
    assert dict(checkpoint.last_error_detail) == caught.value.detail


def test_error_diagnostics_drop_unbounded_and_unknown_fields():
    error = WebAdLandingCaptureError(
        reason_code="WEB_AD_PAGE_OPEN_FAILED", message="失败",
        detail={"cause_type": "x" * 129, "raw_error_code": "https://secret.invalid/",
                "surface_phase_run_id": "surface_phase_run:secret", "url": "secret"},
    )
    assert error.detail == {}


@pytest.mark.asyncio
async def test_malformed_error_code_never_enters_terminal_reason():
    claims = Claims()
    with pytest.raises(WebAdLandingCaptureError) as caught:
        await capture_failure(BlockedPageCaptureActionError(
            "失败", error_code="WEB_AD_ https://secret.invalid/?token=secret",
        ), claims)
    assert caught.value.reason_code == "WEB_AD_PAGE_OPEN_FAILED"
    assert "secret" not in json.dumps(claims.finished)


@pytest.mark.asyncio
async def test_isolation_error_keeps_raw_code_while_mapping_business_reason():
    claims = Claims()
    with pytest.raises(WebAdLandingCaptureError) as caught:
        await capture_failure(BlockedPageCaptureActionError(
            "失败", error_code="EXTERNAL_NAVIGATION_ISOLATION_FAILED",
        ), claims)
    assert caught.value.reason_code == "WEB_AD_LANDING_RETURN_FAILED"
    assert caught.value.detail["raw_error_code"] == "EXTERNAL_NAVIGATION_ISOLATION_FAILED"
    assert claims.finished[0]["reason_code"] == "WEB_AD_LANDING_RETURN_FAILED"


@pytest.mark.asyncio
async def test_failure_finish_uses_atomic_step_with_hash_without_mutating_claim_context():
    from services.api.app.domain.exploration.surface_phase import build_surface_phase_payload_hash

    calls = []

    class Connection:
        async def execute(self, query, *args):
            calls.append((query, args))

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    repository = PostgresWebAdOutboundClaimRepository(pool=Pool())
    await repository.finish(
        phase_id=PHASE, status="failed", reason_code=DENIED, artifact_ids=(),
        detail={"cause_type": "BlockedPageCaptureActionError", "raw_error_code": DENIED,
                "url": "https://secret.invalid/"},
    )
    assert len(calls) == 1
    sql, args = calls[0]
    assert "WITH finished AS" in sql and "INSERT INTO qc_surface_phase_step" in sql
    assert "status='running'" in sql and "context_payload" not in sql
    diagnostic = json.loads(args[5])
    assert diagnostic == {"cause_type": "BlockedPageCaptureActionError", "raw_error_code": DENIED}
    assert args[6] == build_surface_phase_payload_hash(diagnostic)
    assert "secret" not in str(args)


@pytest.mark.asyncio
async def test_legacy_finish_without_detail_keeps_original_write_contract():
    calls = []

    class Connection:
        async def execute(self, query, *args):
            calls.append((query, args))

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    await PostgresWebAdOutboundClaimRepository(pool=Pool()).finish(
        phase_id=PHASE, status="completed", reason_code="WEB_AD_LANDING_OBSERVED", artifact_ids=(),
    )
    assert len(calls) == 1
    assert len(calls[0][1]) == 4
    assert "qc_surface_phase_step" not in calls[0][0]
