"""031会员外壳的隔离回放边界；不把模拟接口证明为原站只读接口。"""

import asyncio
from dataclasses import fields, replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.evidence_recovery_replay import EvidenceRecoveryReplayError
from services.api.app.application.h5_targeted_recapture import H5TargetedRecapturePlanService
from services.api.app.domain.inspection import (
    EvidenceInteractionRequirement,
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSourceGrade,
    RestorationStrategy,
    build_evidence_request,
)
from services.api.app.domain.interaction import InteractionOperation
from services.api.tests.test_h5_closed_session_recapture import _closed_context, _new_runtime
from services.api.tests.test_h5_probe_display_safety_regressions import PLAN
from services.api.tests.test_h5_probe_journey_browser import execute
from services.api.tests.test_h5_probe_journey_browser import page as page
from services.api.tests.test_h5_targeted_recapture import NOW, _snapshot_request


def _payment_request(**changes):
    original = _snapshot_request()
    payload = {
        field.name: getattr(original, field.name)
        for field in fields(original)
        if field.name not in {"request_id", "requirement_hash", "protocol_version"}
    }
    payload["required_interaction"] = EvidenceInteractionRequirement(
        operation=InteractionOperation.CLICK,
        semantic_target="选择当前会员档位",
        purpose="只读展示补证",
        parameters={"goal_capability": "payment_plan_selection"},
    )
    return build_evidence_request(**{**payload, **changes})


async def test_active_payment_uses_authorized_isolated_replay_not_main_surface():
    current = replace(
        _closed_context(), source_session_closed=False, current_capture_id="capture-target"
    )
    replay = replace(_closed_context(), source_session_closed=False)
    runtime = SimpleNamespace(
        remember_new_session_plan=AsyncMock(), deadline=NOW + timedelta(minutes=1)
    )
    service = H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(
            read=AsyncMock(return_value=current),
            read_isolated_display_context=AsyncMock(return_value=replay),
        ),
        clock=lambda: NOW,
        new_session_runtime=runtime,
    )
    result = await service.resolve(
        request=_payment_request(), agent_run_id="run-h5", finalization_epoch=4
    )
    assert result.executable
    assert result.plan.strategy is RestorationStrategy.NEW_SESSION_REPLAY
    assert result.plan.target_session_id != current.session_id
    assert result.plan.source_grade is EvidenceSourceGrade.C
    assert result.plan.deadline == runtime.deadline
    bound = runtime.remember_new_session_plan.call_args.kwargs["context"]
    assert bound.source_session_closed is False
    assert bound.isolated_display is True


@pytest.mark.parametrize(
    "change",
    [
        {"session_dependency": EvidenceSessionDependency.ACTIVE_SESSION},
        {"replay_policy": EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED},
        {"required_equivalence": EvidenceRequiredEquivalence.ORIGINAL_ATOMIC},
        {"minimum_source_grade": EvidenceSourceGrade.A1},
    ],
)
async def test_payment_isolation_never_weakens_frozen_source_contract(change):
    current = replace(
        _closed_context(), source_session_closed=False, current_capture_id="capture-target"
    )
    runtime = SimpleNamespace(remember_new_session_plan=AsyncMock())
    reader = AsyncMock(return_value=current)
    service = H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(
            read=AsyncMock(return_value=current), read_isolated_display_context=reader
        ),
        clock=lambda: NOW,
        new_session_runtime=runtime,
    )
    result = await service.resolve(
        request=_payment_request(**change), agent_run_id="run-h5", finalization_epoch=4
    )
    if change.get("required_equivalence") is EvidenceRequiredEquivalence.ORIGINAL_ATOMIC:
        assert not result.executable
        assert result.reason_code == "H5_RECAPTURE_REQUEST_CONSTRAINT_UNSUPPORTED"
    else:
        assert result.executable
        assert result.plan.strategy is RestorationStrategy.ORIGINAL_SURFACE_PIN
        assert result.plan.target_session_id == current.session_id
    reader.assert_not_awaited()
    runtime.remember_new_session_plan.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"task_id": "other"},
        {"agent_run_id": "other"},
        {"finalization_epoch": 5},
        {"session_id": "h5:other"},
        {"replay_session_id": "h5:run-h5"},
    ],
)
async def test_payment_isolation_rejects_foreign_or_shared_context(change):
    current = replace(
        _closed_context(), source_session_closed=False, current_capture_id="capture-target"
    )
    replay = replace(_closed_context(), source_session_closed=False, **change)
    runtime = SimpleNamespace(remember_new_session_plan=AsyncMock())
    service = H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(
            read=AsyncMock(return_value=current),
            read_isolated_display_context=AsyncMock(return_value=replay),
        ),
        clock=lambda: NOW,
        new_session_runtime=runtime,
    )
    result = await service.resolve(
        request=_payment_request(), agent_run_id="run-h5", finalization_epoch=4
    )
    assert not result.executable
    runtime.remember_new_session_plan.assert_not_awaited()


async def test_new_session_runtime_accepts_active_isolated_plan_and_only_closes_owned_session():
    runtime, plan, browser, _, _ = await _new_runtime()
    original = runtime._plans[plan.plan_id]
    await runtime.remember_new_session_plan(
        plan=plan,
        context=replace(
            original,
            source_session_closed=False,
            isolated_display=True,
        ),
    )
    await runtime.prepare_new_session(plan=plan, agent_run_id="run-h5", finalization_epoch=4)
    await runtime.close_owned_sessions()
    browser.close_h5_exploration_session.assert_awaited_once_with(
        task_id=plan.task_id,
        session_id=plan.target_session_id,
    )


async def test_new_session_runtime_does_not_infer_active_isolation_from_session_name():
    runtime, plan, browser, _, _ = await _new_runtime()
    original = runtime._plans[plan.plan_id]
    with pytest.raises(EvidenceRecoveryReplayError, match="H5_NEW_SESSION_PLAN_SCOPE_INVALID"):
        await runtime.remember_new_session_plan(
            plan=plan,
            context=replace(original, source_session_closed=False),
        )
    browser.surface_driver.start_session.assert_not_awaited()


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_unknown_catalog_read_name_is_not_readonly_proof(page, method):
    seen = []

    async def backend(route):
        seen.append(route.request.url)
        await route.fulfill(body='{"selected":true}', content_type="application/json")

    await page.context.route("https://fixture.invalid/**", backend)
    await page.set_content(PLAN)
    await page.evaluate(
        """method=>{month.onclick=()=>{
      month.classList.add('selected');fetch('https://fixture.invalid/catalog/read',{method})
        .catch(()=>{});
    }}""",
        method,
    )
    trace, _, _ = await execute(page, "payment_plan_selection", "payment")
    assert seen == []
    assert trace[-1]["result"] == "probe_effect_unverified"
    assert trace[-1]["state_diff"]["h5_probe_effect"]["readonly_denied_count"] > 0


async def test_probe_context_blocks_late_orders_without_blocking_main_spa_video_requests(page):
    seen = []

    async def backend(route):
        seen.append(route.request.url)
        await route.fulfill(body='{"ready":true}', content_type="application/json")

    await page.context.route("https://fixture.invalid/**", backend)
    await page.set_content("<main id='view'>首页</main>")
    context = await page.context.browser.new_context(viewport={"width": 390, "height": 600})
    try:
        probe = await context.new_page()
        from services.api.app.infrastructure.browser.h5_readonly_display import (
            install_h5_readonly_display_guard,
        )

        await install_h5_readonly_display_guard(context)
        await context.route("https://fixture.invalid/**", backend)
        await probe.set_content(
            PLAN
            + """<script>month.onclick=()=>{
          month.classList.add('selected');setTimeout(()=>
            fetch('https://fixture.invalid/order',{method:'POST'}).catch(()=>{}),1600);
        }</script>"""
        )
        trace, _, _ = await execute(probe, "payment_plan_selection", "payment")
        assert trace[-1]["result"] == "probe_effect_observed"
        await page.evaluate("""async()=>{
          view.innerHTML='<video id="player" controls></video>';
          await fetch('https://fixture.invalid/media-metadata');
          await fetch('https://fixture.invalid/payment-catalog');
        }""")
        await asyncio.sleep(1.8)
        assert seen == [
            "https://fixture.invalid/media-metadata",
            "https://fixture.invalid/payment-catalog",
        ]
        assert await page.locator("video").count() == 1
        assert page.context._alioth_h5_readonly_display_guard.page is None
    finally:
        await context.close()


@pytest.mark.parametrize("restorable", [True, False])
async def test_main_drain_defers_only_proven_payment_replay_without_claiming_or_sealing(restorable):
    from contextlib import nullcontext

    from services.api.app.application.h5_targeted_recapture import (
        H5TargetedRecaptureDisposition,
        H5TargetedRecaptureDrainService,
    )
    from services.api.app.domain.exploration import ExplorationBudget

    request = _payment_request()
    unrelated = _snapshot_request()
    consumer = SimpleNamespace(
        can_defer_snapshot_requests=AsyncMock(return_value=restorable),
        consume_next=AsyncMock(
            return_value=SimpleNamespace(
                disposition=H5TargetedRecaptureDisposition.EMPTY,
                request=None,
            )
        ),
    )
    reader = SimpleNamespace(
        read_h5_epoch=AsyncMock(return_value=4),
        list_deferred_snapshot_requests=AsyncMock(return_value=(request, unrelated)),
    )
    drain = H5TargetedRecaptureDrainService(
        consumer=consumer,
        scope_reader=reader,
        budget=ExplorationBudget(
            max_actions=80, remaining_actions=12, max_depth=3, remaining_duration_ms=10_000
        ),
        observation_sharing=SimpleNamespace(share_observations=nullcontext),
    )
    await drain.drain_one(
        task_id=request.task_id,
        agent_run_id="run-h5",
        deadline=NOW + timedelta(minutes=1),
        include_snapshot=True,
    )
    consumer.can_defer_snapshot_requests.assert_awaited_once_with(
        requests=(request,),
        agent_run_id="run-h5",
        finalization_epoch=4,
    )
    arguments = consumer.consume_next.call_args.kwargs
    assert arguments.get("deferred_snapshot_request_ids", ()) == (
        (request.request_id,) if restorable else ()
    )
    assert arguments["budget"].resolved_remaining_business_actions == 12
