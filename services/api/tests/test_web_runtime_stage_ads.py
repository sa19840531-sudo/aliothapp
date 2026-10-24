"""广告阶段复用持久化回执，不重放隔离浏览器动作。"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.application.web_ad_landing_evidence import WebAdLandingCaptureReceipt
from services.api.app.domain.exploration.web_run_stage_checkpoint import (
    WebRunStage,
    WebStageCursor,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.tests.test_web_runtime_stage_effects import (
    ARTIFACT,
    CAPTURE,
    RUN,
    command,
    cursor,
)


def ad_cursor(stage):
    current = cursor(stage)
    return WebStageCursor.start(
        run_id=current.run_id,
        logical_page_key=current.logical_page_key,
        stage=current.current_stage,
        cursor_payload=current.cursor_payload,
    ).claim(worker_id="worker", now=current.claimed_at)


class HomeEvidence:
    async def capture_outcome(self, *, capture_id):
        assert capture_id == CAPTURE
        return {"page_type": "home"}


def _persisted_ad_artifacts():
    return [
        {"id": "source-dom", "artifact_type": "dom", "metadata": {"encoding": "utf-8"}},
        {
            "id": "pair",
            "artifact_type": "screenshot",
            "metadata": {"purpose": "ad_landing_consistency"},
        },
        {
            "id": "landing-dom",
            "artifact_type": "dom",
            "metadata": {"purpose": "ad_landing"},
        },
        {
            "id": "landing-shot",
            "artifact_type": "screenshot",
            "metadata": {"purpose": "ad_landing"},
        },
        {
            "id": "landing-ui",
            "artifact_type": "ui_snapshot",
            "metadata": {"purpose": "ad_landing"},
        },
        {
            "id": "landing-har",
            "artifact_type": "har",
            "metadata": {"purpose": "ad_landing"},
        },
        {"id": "landing-trace", "artifact_type": "action_trace", "metadata": {}},
    ]


async def test_ad_claim_accepts_newer_same_page_capture_after_primary_scroll():
    """广告独立页可引用判定帧，但领取时必须验签同页的最新主会话帧。"""

    from services.api.app.infrastructure.db.web_ad_outbound_claim_repository import (
        PostgresWebAdOutboundClaimRepository,
    )

    current_capture_id = "11111111-1111-4111-8111-111111111119"
    calls = []

    class Connection:
        async def fetchval(self, sql, *args):
            calls.append((sql, args))
            return "phase-1"

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    permit = SessionCommandPermit(
        task_id="11111111-1111-4111-8111-111111111111",
        session_id="web:22222222-2222-4222-8222-222222222222",
        surface_kind=SurfaceKind.WEB,
        lease_owner="worker",
        lease_token="33333333-3333-4333-8333-333333333333",
        command_sequence=10,
        finalization_epoch=0,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        current_capture_id=current_capture_id,
    )
    command = SimpleNamespace(
        task_id=permit.task_id,
        agent_run_id="22222222-2222-4222-8222-222222222222",
        session_id=permit.session_id,
        logical_page_id="home",
        source_capture_id=CAPTURE,
        source_url="https://example.test/",
        destination_url="https://ads.example.test/",
        page_role="home",
    )

    assert (
        await PostgresWebAdOutboundClaimRepository(pool=Pool()).claim(
            command=command,
            permit=permit,
        )
        == "phase-1"
    )
    sql, args = calls[0]
    assert "session.current_capture_id=capture.id" not in sql
    assert "session.current_capture_id=$13::uuid" in sql
    assert "current_page.url=$14" in sql
    assert args[12:] == (current_capture_id, command.source_url)


async def test_ad_capture_and_closeout_only_read_durable_receipt():
    from services.api.app.application.web_runtime_stage_ads import WebRuntimeStageAds

    calls = []

    class Port:
        async def read_terminal(self, request):
            calls.append("read")
            return WebAdLandingCaptureReceipt(
                capture_id=CAPTURE,
                landing_url="https://ads.example/",
                artifact_ids=(ARTIFACT,),
                artifact_refs=(),
                source_restored=True,
            )

        async def capture(self, request):
            raise AssertionError("已持久化广告不能再次打开")

    adapter = WebRuntimeStageAds(
        runtime=SimpleNamespace(stage_ad_capture_port=Port()),
        command=command(),
        evidence=HomeEvidence(),
    )
    for stage in (
        WebRunStage.AD_ISOLATED_NAVIGATION,
        WebRunStage.AD_CAPTURE,
        WebRunStage.AD_CLOSEOUT,
    ):
        current = ad_cursor(stage)
        result = await adapter.execute(current)
        assert result.artifact_ids == (ARTIFACT,)
        assert result.cursor_payload.get("capture_id") is None
    assert calls == ["read", "read", "read"]


async def test_ad_open_failure_cannot_manufacture_capture_evidence():
    from services.api.app.application.web_run_stage_orchestrator import WebStageFailure
    from services.api.app.application.web_runtime_stage_ads import WebRuntimeStageAds

    class Port:
        async def read_terminal(self, request):
            return None

    adapter = WebRuntimeStageAds(
        runtime=SimpleNamespace(stage_ad_capture_port=Port()),
        command=command(),
        evidence=HomeEvidence(),
    )
    with pytest.raises(WebStageFailure, match="WEB_AD"):
        await adapter.execute(ad_cursor(WebRunStage.AD_CAPTURE))


async def test_outbound_repository_recovers_isolated_capture_before_phase_finish():
    from services.api.app.application.web_runtime_stage_ads import WebRuntimeStageAds
    from services.api.app.infrastructure.db.web_ad_outbound_claim_repository import (
        PostgresWebAdOutboundClaimRepository,
    )

    class Connection:
        async def fetchrow(self, sql, *args):
            assert "session_command_sequence=phase.command_sequence" in sql
            assert "finalization_epoch" in sql and "source_capture_id" in sql
            return {
                "status": "running",
                "terminal_reason_code": None,
                "capture_id": CAPTURE,
                "url": "https://ads.example/",
                "page_type": "ad_page",
                "artifacts": _persisted_ad_artifacts(),
                "action_log": [
                    {
                        "action": "navigate",
                        "navigation_mode": "isolated_context_goto",
                        "navigation": {"new_page_count": 1, "closed_after_observation_count": 1},
                        "ad_navigation_evidence": {
                            "status": "observed",
                            "landing": {"url": "https://ads.example/"},
                        },
                    }
                ],
            }

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    adapter = WebRuntimeStageAds(
        runtime=SimpleNamespace(), command=command(), evidence=HomeEvidence()
    )
    result = await PostgresWebAdOutboundClaimRepository(pool=Pool()).read_terminal(
        command=await adapter._request(ad_cursor(WebRunStage.AD_CAPTURE))
    )
    assert result.capture_id == CAPTURE
    assert result.source_restored and result.page_type == "ad_page"
    assert result.artifact_ids == tuple(item["id"] for item in _persisted_ad_artifacts())
    assert dict(result.artifact_refs) == {
        "source_dom": "source-dom",
        "source_screenshot": "pair",
        "landing_dom": "landing-dom",
        "landing_screenshot": "landing-shot",
        "landing_ui_snapshot": "landing-ui",
        "landing_har": "landing-har",
        "landing_trace": "landing-trace",
    }


@pytest.mark.parametrize("missing_index", range(7))
async def test_outbound_repository_rejects_recovery_missing_any_required_artifact(
    missing_index,
):
    """Worker 重启恢复必须完整命中七类角色，旧的半套证据不能冒充成功。"""

    from services.api.app.application.web_ad_landing_evidence import (
        WebAdLandingCaptureError,
    )
    from services.api.app.application.web_runtime_stage_ads import WebRuntimeStageAds
    from services.api.app.infrastructure.db.web_ad_outbound_claim_repository import (
        PostgresWebAdOutboundClaimRepository,
    )

    artifacts = _persisted_ad_artifacts()
    del artifacts[missing_index]

    class Connection:
        async def fetchrow(self, _sql, *_args):
            return {
                "status": "running",
                "terminal_reason_code": None,
                "capture_id": CAPTURE,
                "url": "https://ads.example/",
                "page_type": "ad_page",
                "artifacts": artifacts,
                "action_log": [
                    {
                        "action": "navigate",
                        "navigation_mode": "isolated_context_goto",
                        "navigation": {
                            "new_page_count": 1,
                            "closed_after_observation_count": 1,
                        },
                        "ad_navigation_evidence": {
                            "status": "observed",
                            "landing": {"url": "https://ads.example/"},
                        },
                    }
                ],
            }

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    adapter = WebRuntimeStageAds(
        runtime=SimpleNamespace(), command=command(), evidence=HomeEvidence()
    )
    with pytest.raises(WebAdLandingCaptureError) as exc_info:
        await PostgresWebAdOutboundClaimRepository(pool=Pool()).read_terminal(
            command=await adapter._request(ad_cursor(WebRunStage.AD_CAPTURE))
        )
    assert exc_info.value.reason_code == "WEB_AD_LANDING_OPEN_EVIDENCE_INVALID"


@pytest.mark.parametrize(
    ("logical_page_key", "page_type", "expected_role"),
    (
        ("home", "home", "home"),
        ("menu-random", "content_list", "menu"),
        ("video-random", "video_list", "menu"),
        ("detail:menu-random", "content_detail", "detail"),
        ("detail:video-random", "video_detail", "detail"),
    ),
)
async def test_ad_stage_carries_trusted_business_page_type_and_role(
    logical_page_key, page_type, expected_role
):
    """广告阶段必须从可信 Capture 分类取业务角色，不能靠逻辑键猜成 menu。"""

    from services.api.app.application.web_runtime_stage_ads import WebRuntimeStageAds

    class Evidence:
        async def capture_outcome(self, *, capture_id):
            assert capture_id == CAPTURE
            return {"page_type": page_type}

    class Snapshots:
        def __init__(self):
            self.capture_ids = []

        async def read_snapshot(self, *, capture_id):
            self.capture_ids.append(capture_id)
            assert capture_id == CAPTURE
            return {
                "elements": [
                    {
                        "tag": "a",
                        "href": "https://ads.other.test/promo",
                        "text": "广告",
                        "class_name": "banner",
                        "visible": True,
                    }
                ]
            }

    class Checkpoints:
        async def list_for_run(self, *, run_id):
            return ()

        async def create(self, item):
            self.created = item
            return item

    snapshots = Snapshots()
    runtime = SimpleNamespace(
        ad_snapshot_reader=snapshots,
        stage_ad_capture_port=SimpleNamespace(),
        ad_registered_domain="example.test",
    )
    checkpoints = Checkpoints()
    adapter = WebRuntimeStageAds(runtime=runtime, evidence=Evidence(), command=command())
    source = WebStageCursor.start(
        run_id=RUN,
        logical_page_key=logical_page_key,
        stage=WebRunStage.HREF_DISCOVERY,
        cursor_payload={
            "session_id": f"web:{RUN}",
            "finalization_epoch": 2,
            "capture_id": CAPTURE,
            "parent_capture_id": "pre-clear-capture",
            "current_url": "https://example.test/random",
        },
    )

    await adapter.schedule(source, checkpoints)

    assert snapshots.capture_ids == [CAPTURE]
    assert "page_role" not in checkpoints.created.cursor_payload
    assert "page_type" not in checkpoints.created.cursor_payload
    request = await adapter._request(checkpoints.created)
    assert request.page_role == expected_role
    assert request.page_type == page_type


@pytest.mark.parametrize(
    "page_type",
    ("age_gate", "ad_page", "search_page", "activity", "unknown", None),
)
async def test_ad_stage_does_not_schedule_non_business_or_unclassified_page(page_type):
    from services.api.app.application.web_runtime_stage_ads import WebRuntimeStageAds

    class Evidence:
        async def capture_outcome(self, **_kwargs):
            return {"page_type": page_type}

    class Checkpoints:
        async def create(self, _item):
            raise AssertionError("非业务页不得递归或猜测广告角色")

    class Snapshots:
        async def read_snapshot(self, **_kwargs):
            return {
                "elements": [
                    {
                        "href": "https://ads.other.test/promo",
                        "class_name": "banner",
                    }
                ]
            }

    runtime = SimpleNamespace(
        ad_snapshot_reader=Snapshots(),
        stage_ad_capture_port=SimpleNamespace(),
        ad_registered_domain="example.test",
    )
    adapter = WebRuntimeStageAds(runtime=runtime, evidence=Evidence(), command=command())
    source = WebStageCursor.start(
        run_id=RUN,
        logical_page_key="random",
        stage=WebRunStage.HREF_DISCOVERY,
        cursor_payload={
            "session_id": f"web:{RUN}",
            "finalization_epoch": 2,
            "capture_id": CAPTURE,
            "current_url": "https://example.test/random",
        },
    )

    await adapter.schedule(source, Checkpoints())
