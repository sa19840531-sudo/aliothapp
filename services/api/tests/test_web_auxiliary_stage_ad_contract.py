"""辅助导航实际业务页必须接入统一广告阶段，不扩大菜单探索范围。"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from services.api.app.application.web_ad_landing_evidence import (
    WebAdLandingCaptureReceipt,
    WebAdLandingEvidenceScheduler,
)
from services.api.app.application.web_auxiliary_navigation import WebAuxiliaryNavigationExplorer
from services.api.app.application.web_run_stage_orchestrator import (
    WebRunStageOrchestrator,
    WebStageEffectResult,
    WebStageFailure,
)
from services.api.app.application.web_runtime_stage_effects import WebRuntimeStageEffects
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    WebStageCheckpointConflict,
)
from services.api.tests.test_web_runtime_stage_effects import RUN, command
from services.api.tests.test_web_runtime_stage_effects import cursor as stage_cursor
from services.api.tests.web_runtime_recovery_fixture import RecoveryWorld

HOME = "https://example.test/"
SEARCH = "https://example.test/search"
ACTIVITY = "https://example.test/campaign"
SHARED_AD = "https://ads.other.test/shared"
AUX_AD = "https://ads.other.test/auxiliary"


class _WorkerStopped(BaseException):
    """模拟进程中断，不能被业务可选失败分支吞掉。"""


def _world(monkeypatch, *, page_type="content_list", distinct_ad=True, shared_in_aux=True):
    world = RecoveryWorld(monkeypatch, with_candidates=True)
    world.ad_requests, world.ad_receipts, world.legacy_attempts = [], {}, []
    world.aux_claims, world.aux_finished = {}, []
    world.freeze_sources = []
    world.ad_states_at_navigation = {}
    world.classification_failure = None
    world.stop_before_classification = False
    world.stop_after_classification = False
    world.workflow_command = replace(command(), deadline=world.now + timedelta(minutes=180))

    class Snapshots:
        async def read_snapshot(self, *, capture_id):
            url = world.captures[capture_id]["url"]
            elements = []
            if url == HOME:
                elements.append({"tag": "a", "text": "搜索", "href": "/search"})
            if url == SEARCH:
                elements.append({"tag": "a", "text": "活动", "href": "/campaign"})
            if url == HOME or (url == SEARCH and shared_in_aux):
                elements.append({"tag": "a", "class_name": "banner", "href": SHARED_AD})
            if url == SEARCH and distinct_ad:
                elements.append({"tag": "a", "class_name": "banner", "href": AUX_AD})
            return {
                "capture_id": capture_id,
                "url": url,
                "elements": [
                    {"selector": f"#item-{index}", "visible": True, **element}
                    for index, element in enumerate(elements)
                ],
            }

        async def read_url(self, *, capture_id):
            return world.captures[capture_id]["url"]

    class Coordinator:
        async def coordinate(self, request):
            intent = request.intent
            assert intent.source_capture_id == world.current, world.actions
            world.ad_states_at_navigation[intent.parameters["destination_url"]] = tuple(
                (row.cursor_payload.get("requested_url"), row.current_stage, row.is_terminal)
                for row in world.rows.values()
                if row.logical_page_key.startswith("ad:")
            )
            world.actions.append(intent.parameters["destination_url"])
            capture_id = world.capture(intent.parameters["destination_url"], "probe")
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id=capture_id))

    class Classifier:
        async def classify(self, ready):
            if ready.url == SEARCH and world.stop_before_classification:
                world.stop_before_classification = False
                raise _WorkerStopped()
            kind = (
                "home" if ready.url == HOME else
                page_type if ready.url == SEARCH else
                "activity" if ready.url == ACTIVITY else "content_detail"
            )
            world.classifications.append(ready.url)
            world.captures[ready.capture_id]["page_type"] = kind
            if ready.url == SEARCH:
                if world.classification_failure is not None:
                    raise world.classification_failure
                if world.stop_after_classification:
                    world.stop_after_classification = False
                    raise _WorkerStopped()
            return SimpleNamespace(page_type=kind, raw_page_type=kind)

    class Claims:
        async def claim(self, **values):
            key = (values["phase_key"], values["source_capture_id"], values["candidate_id"])
            if key in world.aux_claims:
                return None
            phase_id = str(uuid5(NAMESPACE_URL, ":".join(key)))
            world.aux_claims[key] = phase_id
            return phase_id

        async def finish(self, **values):
            world.aux_finished.append(values)

    class AdPort:
        async def read_terminal(self, request):
            return world.ad_receipts.get(request.logical_page_id)

        async def capture(self, request):
            assert request.source_capture_id == world.current
            assert request.source_url == world.captures[world.current]["url"]
            world.ad_requests.append(request)
            world.actions.append("ad:" + request.destination_url)
            receipt = WebAdLandingCaptureReceipt(
                capture_id=str(uuid5(NAMESPACE_URL, request.logical_page_id)),
                landing_url=request.destination_url,
                requested_url=request.destination_url,
                artifact_ids=(),
                artifact_refs=(),
                source_restored=True,
            )
            world.ad_receipts[request.logical_page_id] = receipt
            return receipt

    class LegacyScheduler(WebAdLandingEvidenceScheduler):
        async def attempt(self, page):
            world.legacy_attempts.append(page)
            return await super().attempt(page)

    source = world.runtime.navigation_candidate_source
    original_candidates = source.navigation_candidates

    async def candidates(session_id):
        world.freeze_sources.append(world.current)
        return tuple(
            item for item in await original_candidates(session_id) if "healthy" in item.url
        )

    source.navigation_candidates = candidates
    snapshots, classifier, port = Snapshots(), Classifier(), AdPort()
    world.runtime.page_type_classifier = classifier
    world.runtime.ad_snapshot_reader = snapshots
    world.runtime.ad_registered_domain = "example.test"
    world.runtime.stage_ad_capture_port = port
    world.runtime.auxiliary_navigation_explorer = WebAuxiliaryNavigationExplorer(
        coordinator=Coordinator(),
        snapshot_reader=snapshots,
        capture_url_reader=snapshots,
        readiness=world.runtime.page_readiness,
        ready_capture_reader=world.runtime.stage_ready_capture_reader,
        classifier=classifier,
        claims=Claims(),
        ad_landing_scheduler=LegacyScheduler(capture_port=port),
    )
    return world


async def _run(world):
    """真实阶段图最多推进一百步，超时只限制测试，不替换 Run 冻结期限。"""
    runner = world.runner()
    world.runtime.stage_orchestrator = runner
    await runner.initialize(world.workflow_command, adult_self_attestation=None)
    for _ in range(100):
        due = await runner.next_due(RUN)
        if due is None:
            return await runner.final_result(RUN)
        await runner.advance_once(RUN, due.logical_page_key)
    raise AssertionError("辅助广告没有在有界阶段数内完成收口")


@pytest.mark.parametrize(
    "page_type", ["home", "content_list", "content_detail", "video_list", "video_detail"]
)
@pytest.mark.parametrize("shared_in_aux", [False, True])
async def test_auxiliary_business_page_closes_stage_ad_before_leaving(
    monkeypatch, page_type, shared_in_aux,
):
    world = _world(monkeypatch, page_type=page_type, shared_in_aux=shared_in_aux)
    await asyncio.wait_for(_run(world), timeout=5)

    selected = SHARED_AD if shared_in_aux else AUX_AD
    assert [request.destination_url for request in world.ad_requests] == [SHARED_AD, selected]
    assert [request.logical_page_id for request in world.ad_requests] == [
        "ad:home", "ad:aux:search",
    ]
    assert world.actions.index("ad:" + selected) < world.actions.index(ACTIVITY)
    assert (selected, WebRunStage.AD_CLOSEOUT, True) in world.ad_states_at_navigation[ACTIVITY]
    assert all(request.deadline == world.workflow_command.deadline for request in world.ad_requests)
    assert world.legacy_attempts == []
    aux_ads = [
        row for row in world.rows.values() if row.logical_page_key == "ad:aux:search"
    ]
    assert len(aux_ads) == 1
    assert aux_ads[0].current_stage is WebRunStage.AD_CLOSEOUT and aux_ads[0].is_terminal
    assert len(world.freeze_sources) == 1
    assert len(world.claimed) == 1
    assert all("search" not in entry and "campaign" not in entry for entry in world.claimed)


async def test_auxiliary_shared_href_never_falls_back_to_legacy_direct_open(monkeypatch):
    world = _world(monkeypatch, distinct_ad=False)
    await asyncio.wait_for(_run(world), timeout=5)

    assert [request.destination_url for request in world.ad_requests] == [SHARED_AD, SHARED_AD]
    assert len({request.logical_page_id for request in world.ad_requests}) == 2
    assert world.legacy_attempts == []
    assert world.actions.count(SEARCH) == 1
    assert world.closed


async def test_auxiliary_navigation_preserves_home_freeze_and_menu_claim_scope(monkeypatch):
    world = _world(monkeypatch)
    await asyncio.wait_for(_run(world), timeout=5)

    assert len(world.freeze_sources) == 1
    assert world.captures[world.freeze_sources[0]]["url"] == HOME
    assert len(world.claimed) == 1
    assert all("search" not in entry and "campaign" not in entry for entry in world.claimed)
    assert world.actions.count(SEARCH) == 1
    assert world.actions.count(ACTIVITY) == 1


@pytest.mark.parametrize("page_type", ["search_page", "activity", "unknown"])
async def test_non_business_auxiliary_page_does_not_open_its_banner(monkeypatch, page_type):
    world = _world(monkeypatch, page_type=page_type)
    await asyncio.wait_for(_run(world), timeout=5)

    assert [request.destination_url for request in world.ad_requests] == [SHARED_AD]
    assert world.closed


@pytest.mark.parametrize(
    "failure", [WebStageCheckpointConflict("失去阶段所有权"), RuntimeError("WEB_STAGE_LEASE_LOST")]
)
async def test_auxiliary_stage_ownership_loss_is_not_downgraded_to_optional_gap(
    monkeypatch, failure,
):
    world = _world(monkeypatch)
    world.classification_failure = failure

    with pytest.raises(type(failure), match=str(failure)):
        await asyncio.wait_for(_run(world), timeout=5)

    assert ACTIVITY not in world.actions
    assert not any("healthy" in item for item in world.actions)
    assert not world.closed


async def test_auxiliary_cancellation_cannot_continue_navigation(monkeypatch):
    world = _world(monkeypatch)
    world.classification_failure = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(_run(world), timeout=5)

    assert ACTIVITY not in world.actions
    assert not any("healthy" in item for item in world.actions)
    assert not world.closed


async def test_resume_after_auxiliary_classification_does_not_lose_ad_or_repeat_goto(monkeypatch):
    world = _world(monkeypatch)
    world.stop_after_classification = True
    with pytest.raises(_WorkerStopped):
        await asyncio.wait_for(_run(world), timeout=5)
    assert world.captures[world.current]["page_type"] == "content_list"
    world.now += timedelta(seconds=601)

    await asyncio.wait_for(_run(world), timeout=5)

    assert [request.destination_url for request in world.ad_requests] == [SHARED_AD, SHARED_AD]
    assert world.classifications.count(SEARCH) == 1
    assert "WEB_STAGE_LEASE_EXPIRED" in world.rows["aux:search"].gap_codes
    assert world.actions.count(SEARCH) == 1
    assert world.legacy_attempts == []
    assert world.closed


async def test_expired_classification_without_durable_result_does_not_create_ad(monkeypatch):
    world = _world(monkeypatch)
    world.stop_before_classification = True
    with pytest.raises(_WorkerStopped):
        await asyncio.wait_for(_run(world), timeout=5)
    world.now += timedelta(seconds=601)
    await asyncio.wait_for(_run(world), timeout=5)
    assert "ad:aux:search" not in world.rows
    assert world.classifications.count(SEARCH) == 0
    assert "WEB_STAGE_LEASE_EXPIRED" in world.rows["aux:search"].gap_codes
    assert world.actions.count(SEARCH) == 1


async def test_auxiliary_ready_page_has_independent_recovery_cursor_before_classification(
    monkeypatch,
):
    world = _world(monkeypatch)
    world.stop_before_classification = True
    with pytest.raises(_WorkerStopped):
        await asyncio.wait_for(_run(world), timeout=5)

    assert world.actions.count(SEARCH) == 1
    assert world.captures[world.current]["page_type"] is None
    resumable = [
        row for row in world.rows.values()
        if row.logical_page_key != "home"
        and not row.logical_page_key.startswith("ad:")
        and row.cursor_payload.get("capture_id") == world.current
        and not row.is_terminal
    ]
    assert len(resumable) == 1, "分类前必须存在不依赖父 FRONTIER_DRAIN 重试的辅助恢复身份"


@pytest.mark.parametrize(
    ("final_url", "reason"),
    [("https://other.test/search", "WEB_NAVIGATION_CROSS_ORIGIN"),
     ("http://example.test/search", "WEB_NAVIGATION_HTTPS_DOWNGRADE"),
     (SEARCH, None)],
)
async def test_auxiliary_completed_navigation_recovery_rechecks_final_url(final_url, reason):
    current = replace(stage_cursor(WebRunStage.BOOTSTRAP_NAVIGATION), logical_page_key="aux:search")
    current = replace(current, cursor_payload={**current.cursor_payload, "requested_url": SEARCH})

    class Evidence:
        async def assert_active(self, cursor):
            assert cursor == current

        async def find_completed(self, cursor):
            return None

        async def find_navigation(self, cursor):
            return WebStageEffectResult(cursor_payload={
                "capture_id": current.cursor_payload["capture_id"], "current_url": final_url,
            })

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(), evidence=Evidence(), command=command(),
    )
    if reason:
        class Checkpoints:
            async def get(self, **kwargs):
                return current

            async def claim(self, **kwargs):
                return current

            async def record_gap_and_advance(self, *args, **kwargs):
                pytest.fail("不安全恢复证据必须 Run 级拒绝，不能伪装为可继续导航的辅助 gap")

        orchestrator = WebRunStageOrchestrator(
            checkpoints=Checkpoints(), effects=effects, worker_id="restore",
        )
        with pytest.raises(WebStageFailure, match=reason):
            await orchestrator.advance_once(RUN, current.logical_page_key)
    else:
        restored = await effects.find_completed(current)
        assert restored.cursor_payload["current_url"] == SEARCH
