"""列表先登记 href；广告排空后才允许详情导航。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from services.api.app.application.web_ad_landing_evidence import WebAdLandingCaptureReceipt
from services.api.app.application.web_list_detail_navigation import WebListDetailResult
from services.api.app.application.web_runtime_stage_ads import WebRuntimeStageAds
from services.api.app.application.web_runtime_stage_discovery import WebRuntimeStageDiscovery
from services.api.app.application.web_runtime_stage_primary import WebRuntimeStagePrimary
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.tests.test_web_runtime_stage_effects import ARTIFACT
from services.api.tests.web_runtime_recovery_fixture import RecoveryWorld


@pytest.mark.parametrize("same_page", [True, False])
async def test_failed_detail_cannot_schedule_ad_from_refreshed_original_list(same_page):
    from services.api.app.application.web_run_stage_orchestrator import WebStageEffectResult
    from services.api.app.domain.exploration.web_run_stage_checkpoint import WebStageCursor

    run_id = str(uuid4())
    source_url = "https://example.test/list"
    cursor = WebStageCursor.start(run_id=run_id, logical_page_key="menu:one", cursor_payload={
        "session_id": f"web:{run_id}", "finalization_epoch": 0,
        "capture_id": str(uuid4()), "current_url": source_url,
    })
    ads = SimpleNamespace(schedule=AsyncMock())
    handler = WebRuntimeStageDiscovery(
        command=SimpleNamespace(), evidence=SimpleNamespace(),
        frontier=SimpleNamespace(detail_authority=AsyncMock(return_value=None)),
        checkpoints=SimpleNamespace(), ads=ads,
    )
    outcome = WebStageEffectResult(cursor_payload={
        "capture_id": str(uuid4()),
        "current_url": source_url if same_page else "https://example.test/other-list",
    })
    await handler.schedule_detail(cursor, outcome)
    assert ads.schedule.await_count == (0 if same_page else 1)


@pytest.mark.parametrize("with_ad", [False, True])
@pytest.mark.parametrize("legacy_marker", [False, True])
@pytest.mark.parametrize("interrupt_before_href", [False, True])
@pytest.mark.parametrize("duplicate_ad", [False, True])
async def test_list_ads_close_before_detail_navigation_with_recovery(
    monkeypatch, with_ad, legacy_marker, interrupt_before_href, duplicate_ad
):
    world = RecoveryWorld(monkeypatch, with_candidates=True)
    order, receipts, opens, primary_captures = [], {}, [], []
    original_candidates = world.runtime.navigation_candidate_source.navigation_candidates

    async def candidates(session_id):
        url = world.captures[world.current]["url"]
        if url.endswith("/healthy"):
            order.append("list_discovery")
        elif url.endswith("/detail"):
            order.append("detail_discovery")
        return await original_candidates(session_id)

    world.runtime.navigation_candidate_source.navigation_candidates = candidates

    class Classifier:
        async def classify(self, ready):
            assert not ready.url.endswith("/detail")
            page_type = "home" if ready.url == "https://example.test/" else "content_list"
            world.captures[ready.capture_id]["page_type"] = page_type
            return SimpleNamespace(page_type=page_type)

    class Primary:
        async def capture(self, **kwargs):
            if kwargs["page_type"] == "content_detail":
                capture_id = world.capture("https://example.test/healthy/detail", "primary")
                world.captures[capture_id]["page_type"] = None
                primary_captures.append(capture_id)
                return SimpleNamespace(status="captured", evidence_capture_id=capture_id)
            return SimpleNamespace(status="captured", evidence_capture_id=kwargs["capture_id"])

    class Details:
        async def capture_one(self, evidence, **kwargs):
            order.append("detail_navigation")
            assert world.captures[world.current]["url"].endswith("/healthy")
            capture_id = world.capture("https://example.test/healthy/detail", "ready")
            world.captures[capture_id]["page_type"] = "content_detail"
            return WebListDetailResult(
                status=WebMenuTerminalStatus.DETAIL_CAPTURED,
                detail_capture_id=capture_id,
                page_type="content_detail",
                detail_action_count=1,
                detail_intent_id="detail-intent",
                detail_action_execution_id="00000000-0000-0000-0000-000000000077",
            )

    class DetailSource:
        async def read_list(self, *, capture_id):
            assert capture_id == world.current
            return SimpleNamespace(ready_capture_id=capture_id)

    class Snapshots:
        async def read_snapshot(self, *, capture_id):
            detail = world.captures[capture_id]["url"].endswith("/detail")
            return (
                {
                    "elements": [
                        {
                            "tag": "a",
                            "href": "https://ads.example/" + (
                                "detail" if detail and not duplicate_ad else "promo"
                            ),
                            "text": "广告",
                            "class_name": "banner",
                            "visible": True,
                        }
                    ]
                }
                if world.captures[capture_id]["url"].endswith(("/healthy", "/detail"))
                else {}
            )

    class AdPort:
        async def read_terminal(self, request):
            return receipts.get(request.logical_page_id)

        async def capture(self, request):
            assert request.source_capture_id == world.current
            assert world.captures[world.current]["url"].endswith(("/healthy", "/detail"))
            opens.append(request.logical_page_id)
            receipts[request.logical_page_id] = WebAdLandingCaptureReceipt(
                capture_id=world.current,
                landing_url=request.destination_url,
                artifact_ids=(ARTIFACT,),
                artifact_refs=(),
                source_restored=True,
            )
            if not request.logical_page_id.startswith("ad:detail:"):
                raise asyncio.CancelledError()
            return receipts[request.logical_page_id]

    original_ad = WebRuntimeStageAds.execute

    async def ad_execute(self, cursor):
        result = await original_ad(self, cursor)
        order.append(cursor.current_stage.value)
        return result

    monkeypatch.setattr(WebRuntimeStageAds, "execute", ad_execute)
    world.runtime.page_type_classifier = Classifier()
    world.runtime.primary_content_capture = Primary()
    world.runtime.stage_detail_navigator = Details()
    world.runtime.stage_detail_evidence_source = DetailSource()
    original_detail = WebRuntimeStagePrimary._capture_detail
    interrupted_detail = False

    async def capture_detail(self, *args, **kwargs):
        nonlocal interrupted_detail
        result = await original_detail(self, *args, **kwargs)
        if not interrupted_detail:
            interrupted_detail = True
            if legacy_marker:
                from services.api.app.application.web_run_stage_orchestrator import (
                    WebStageEffectResult,
                )

                for key, value in tuple(world.completed.items()):
                    if key[1] == "detail":
                        world.completed[key] = WebStageEffectResult(
                            cursor_payload=value.cursor_payload
                        )
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(WebRuntimeStagePrimary, "_capture_detail", capture_detail)
    original_schedule = WebRuntimeStageDiscovery.schedule_detail
    interrupted_schedule = False

    async def schedule_detail(self, *args):
        nonlocal interrupted_schedule
        await original_schedule(self, *args)
        if interrupt_before_href and not interrupted_schedule:
            interrupted_schedule = True
            raise asyncio.CancelledError()

    monkeypatch.setattr(WebRuntimeStageDiscovery, "schedule_detail", schedule_detail)
    if with_ad:
        world.runtime.ad_snapshot_reader = Snapshots()
        world.runtime.stage_ad_capture_port = AdPort()
        world.runtime.ad_registered_domain = "example.test"
        with pytest.raises(asyncio.CancelledError):
            await world.run()
    with pytest.raises(asyncio.CancelledError):
        await world.run()
    if interrupt_before_href:
        with pytest.raises(asyncio.CancelledError):
            await world.run()
    await world.run()
    ad_stages = ["ad_isolated_navigation", "ad_capture", "ad_closeout"]
    expected = ad_stages.copy() if with_ad else []
    expected.append("detail_navigation")
    if with_ad:
        expected.extend(ad_stages)
    # 非首页不再触发菜单发现；详情即使广告相同，也需独立留证，恢复不重复打开。
    assert order == expected
    assert len(opens) == 2 * int(with_ad)
    assert len(primary_captures) == 1
    assert any(key.startswith("detail:") for key in world.rows)
    assert world.closed
