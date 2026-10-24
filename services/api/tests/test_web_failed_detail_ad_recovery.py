"""详情失败经真实阶段、账本权威恢复和广告调度后，不重开原列表广告。"""

from types import SimpleNamespace

import pytest

from services.api.app.application.web_ad_landing_evidence import WebAdLandingCaptureReceipt
from services.api.app.application.web_list_detail_navigation import WebListDetailResult
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.tests.test_web_runtime_stage_effects import ARTIFACT
from services.api.tests.web_runtime_recovery_fixture import RecoveryWorld


@pytest.mark.parametrize("same_page", [True, False])
async def test_failed_detail_refresh_through_runner_ledger_and_ad_scheduler(monkeypatch, same_page):
    world = RecoveryWorld(monkeypatch, with_candidates=True)
    opened = []
    receipts = {}

    class Classifier:
        async def classify(self, ready):
            kind = "home" if ready.url == "https://example.test/" else "content_list"
            world.captures[ready.capture_id]["page_type"] = kind
            return SimpleNamespace(page_type=kind)

    class Details:
        async def capture_one(self, evidence, **kwargs):
            url = "https://example.test/healthy" if same_page else "https://example.test/other-list"
            capture_id = world.capture(url, "failed-detail-refresh")
            world.captures[capture_id]["page_type"] = "content_list"
            return WebListDetailResult(
                status=WebMenuTerminalStatus.DETAIL_PAGE_TYPE_MISMATCH,
                detail_capture_id=capture_id,
                page_type="content_list",
                error_code="WEB_DETAIL_PAGE_TYPE_MISMATCH",
                detail_action_count=1,
            )

    class DetailSource:
        async def read_list(self, *, capture_id):
            assert capture_id == world.current
            return SimpleNamespace(ready_capture_id=capture_id)

    class Snapshots:
        async def read_snapshot(self, *, capture_id):
            if world.captures[capture_id]["page_type"] != "content_list":
                return {}
            return {
                "elements": [
                    {
                        "tag": "a",
                        "href": "https://ads.example/promo",
                        "text": "广告",
                        "class_name": "banner",
                        "visible": True,
                    }
                ]
            }

    class Ads:
        async def read_terminal(self, request):
            return receipts.get(request.logical_page_id)

        async def capture(self, request):
            opened.append(request.logical_page_id)
            receipts[request.logical_page_id] = WebAdLandingCaptureReceipt(
                capture_id=world.current,
                landing_url=request.destination_url,
                artifact_ids=(ARTIFACT,),
                artifact_refs=(),
                source_restored=True,
            )
            return receipts[request.logical_page_id]

    world.runtime.page_type_classifier = Classifier()
    world.runtime.stage_detail_navigator = Details()
    world.runtime.stage_detail_evidence_source = DetailSource()
    world.runtime.ad_snapshot_reader = Snapshots()
    world.runtime.stage_ad_capture_port = Ads()
    world.runtime.ad_registered_domain = "example.test"
    await world.run()
    assert len(opened) == (1 if same_page else 2)
    assert sum(key.startswith("ad:detail:") for key in opened) == int(not same_page)
    failed = [
        record
        for record in world.records.values()
        if record.status is WebMenuTerminalStatus.DETAIL_PAGE_TYPE_MISMATCH
    ]
    assert len(failed) == 1
    assert failed[0].detail_page_type == "content_list"
    assert world.closed
