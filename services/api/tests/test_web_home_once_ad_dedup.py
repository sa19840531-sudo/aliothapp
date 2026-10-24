"""首页菜单冻结与同业务页广告幂等，不跨页吞掉广告采集。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.app.application.web_ad_landing_evidence import select_web_ad_candidate
from services.api.app.application.web_runtime_stage_ads import WebRuntimeStageAds
from services.api.app.application.web_runtime_stage_discovery import WebRuntimeStageDiscovery
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
from services.api.tests.test_web_runtime_stage_effects import CAPTURE, Evidence, command, cursor


async def test_only_home_discovers_menus_but_all_lists_keep_detail_readiness_marker():
    discovered = []
    ads = []

    class Frontier:
        async def discover(self, _command, *, source_capture_id, **_kwargs):
            discovered.append(source_capture_id)

    class Ads:
        async def schedule(self, source, _checkpoints):
            ads.append(source.logical_page_key)

    evidence = Evidence()
    discovery = WebRuntimeStageDiscovery(
        command=command(),
        evidence=evidence,
        frontier=Frontier(),
        checkpoints=SimpleNamespace(),
        ads=Ads(),
    )
    for key in ("home", "discovery:menu", "detail:discovery:menu"):
        source = replace(cursor(WebRunStage.CLASSIFICATION), logical_page_key=key)
        await discovery.before_primary_scroll(source)
        await discovery.before_primary_scroll(source)
        await discovery.before_detail(source, source.cursor_payload)
        await discovery.execute(replace(source, current_stage=WebRunStage.HREF_DISCOVERY))
    assert discovered == [CAPTURE]


@pytest.mark.parametrize("key", ["discovery:menu", "detail:discovery:menu"])
async def test_restored_href_without_marker_does_not_reopen_menu_discovery(key):
    class Frontier:
        async def discover(self, *args, **kwargs):
            pytest.fail("非首页不得重新读取顶部菜单")

    scheduled = []

    class Ads:
        async def schedule(self, source, _checkpoints):
            scheduled.append(source.logical_page_key)

    discovery = WebRuntimeStageDiscovery(
        command=command(),
        evidence=Evidence(),
        frontier=Frontier(),
        checkpoints=SimpleNamespace(),
        ads=Ads(),
    )
    await discovery.execute(replace(cursor(WebRunStage.HREF_DISCOVERY), logical_page_key=key))
    assert scheduled == [key]


def test_candidate_skips_equivalent_consumed_url_but_keeps_distinct_query():
    snapshot = {
        "elements": [
            {"href": "https://ADS.other.test:443/#banner", "class_name": "banner"},
            {"href": "https://ads.other.test/?campaign=b", "class_name": "banner"},
        ]
    }
    selected = select_web_ad_candidate(
        registered_domain="site.test",
        capture_snapshots=[(CAPTURE, snapshot)],
        excluded_urls=("https://ads.other.test/",),
    )
    assert selected.url == "https://ads.other.test/?campaign=b"


@pytest.mark.parametrize("excluded", [(), ("https://bad_host.example/ad",)])
def test_malformed_ad_identity_does_not_block_next_valid_candidate(excluded):
    snapshot = {
        "elements": [
            {"href": "https://bad_host.example/ad", "class_name": "banner"},
            {"href": "https://ads.other.test/good", "class_name": "banner"},
        ]
    }
    selected = select_web_ad_candidate(
        registered_domain="site.test",
        capture_snapshots=[(CAPTURE, snapshot)],
        excluded_urls=excluded,
    )
    assert selected.url == "https://ads.other.test/good"


def test_acceptance_request_uses_full_180_minutes_not_hidden_60_minute_cap():
    from scripts.validation.web_acceptance_source import submission_payload
    from services.api.app.application.v2_task_entries import (
        TaskTargetPlatform,
        _scan_mode_loop_guard,
    )

    payload = submission_payload("https://site.test/")
    guard = _scan_mode_loop_guard(
        payload["scan_mode"],
        platform=TaskTargetPlatform.WEB,
        timeout_minutes=payload["timeout_minutes"],
    )
    assert guard["max_duration_seconds"] == 10800


async def test_observer_accepts_180_minute_window_without_resubmission(monkeypatch):
    from scripts.validation import web_v2_live_acceptance as acceptance

    args = acceptance.build_parser().parse_args(
        [
            "monitor",
            "--site-timeout-minutes",
            "180",
            "--token",
            "test-only",
            "--database-url",
            "postgresql://test.invalid/test",
            "--redis-url",
            "redis://test.invalid",
        ]
    )

    class ReachedLogin(Exception):
        pass

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def login(self):
            raise ReachedLogin

        async def close(self):
            pass

    monkeypatch.setattr(acceptance, "AcceptanceClient", Client)
    with pytest.raises(ReachedLogin):
        await acceptance.run(args)


async def test_ad_schedule_is_per_page_and_never_reassigns_existing_page():
    persisted = []

    class Checkpoints:
        async def list_for_run(self, *, run_id):
            return tuple(row for row in persisted if row.run_id == run_id)

        async def create(self, row):
            persisted.append(row)
            return row

    class Facts:
        page_type = "home"

        async def capture_outcome(self, **kwargs):
            return {"page_type": self.page_type}

    class Snapshots:
        async def read_snapshot(self, **kwargs):
            return {
                "elements": [
                    {"href": "https://ads.other.test/a", "class_name": "banner"},
                    {"href": "https://ads.other.test/b", "class_name": "banner"},
                ]
            }

    facts = Facts()
    runtime = SimpleNamespace(
        ad_snapshot_reader=Snapshots(),
        stage_ad_capture_port=object(),
        ad_registered_domain="example.test",
    )
    for key, kind in (
        ("home", "home"),
        ("home", "home"),
        ("menu", "content_list"),
        ("detail:menu", "content_detail"),
        ("video", "video_list"),
        ("detail:video", "video_detail"),
    ):
        facts.page_type = kind
        # 每次重建适配器，证明去重不能依赖单个 Worker 内存集合。
        adapter = WebRuntimeStageAds(runtime=runtime, evidence=facts, command=command())
        await adapter.schedule(
            replace(cursor(WebRunStage.CLASSIFICATION), logical_page_key=key), Checkpoints()
        )
    assert [(row.logical_page_key, row.cursor_payload["requested_url"]) for row in persisted] == [
        ("ad:home", "https://ads.other.test/a"),
        ("ad:menu", "https://ads.other.test/a"),
        ("ad:detail:menu", "https://ads.other.test/a"),
        ("ad:video", "https://ads.other.test/a"),
        ("ad:detail:video", "https://ads.other.test/a"),
    ]
