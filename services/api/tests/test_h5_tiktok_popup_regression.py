"""TIKTOK保存案例的签发、命中、退场状态与危险目标反例。"""

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.api.app.application.h5_exploration_observation import build_h5_frontier
from services.api.app.application.interaction_runtime import (
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
)
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)
from services.api.tests.test_web_capture_runtime import StaticSnapshotReader


def saved_case():
    return json.loads((Path(__file__).parent / "fixtures/h5_tiktok_3ad88563.json").read_text())


def request():
    case = saved_case()
    action = case["detection"]["candidate_actions"][0]
    return SemanticTargetResolutionRequest(
        task_id=case["provenance"]["task_id"],
        session_id="h5:saved-popup",
        source_capture_id=case["provenance"]["capture_id"],
        action_type=InteractionOperation.CLICK,
        semantic_target=action["target"],
        parameters={
            "target_mode": "popup_dismissal",
            "popup_dismissal_capability": action["obstruction_capability"],
            "obstruction_signature": action["obstruction_signature"],
        },
    )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "external",
        "nested_navigation",
        "miss",
        "hidden",
        "stateful",
        "submit",
        "duplicate",
        "wrong_family",
    ],
)
async def test_saved_vant_ad_dialog_close_requires_unique_safe_hittable_control(fault):
    snapshot = saved_case()["snapshot"]
    close = next(
        item for item in snapshot["elements"] if "van-icon-close" in item.get("class_tokens", [])
    )
    if fault == "external":
        close["data_url"] = "https://outside.invalid/"
    elif fault == "nested_navigation":
        close["navigation_payload_in_subtree"] = True
    elif fault == "miss":
        close["hit_test"]["center_hits_target"] = False
    elif fault == "hidden":
        close["visible"] = False
    elif fault == "stateful":
        close["stateful_control_in_subtree"] = True
    elif fault == "submit":
        close.update(tag="button", type="submit", form_associated=True)
    elif fault == "duplicate":
        other = deepcopy(close)
        other["selector"] += ":nth-of-type(2)"
        snapshot["elements"].append(other)
    elif fault == "wrong_family":
        close["ancestor_class_tokens"] = ["business-card"]
    resolver = UiSnapshotSemanticTargetResolver(snapshot_reader=StaticSnapshotReader(snapshot))
    if fault is not None:
        with pytest.raises(SemanticTargetResolutionError):
            await resolver.resolve(request())
    else:
        target = await resolver.resolve(request())
        assert target.expected_rect == close["rect"]
        assert "van-icon-close" in target.selector


@pytest.mark.parametrize("completed", [False, True])
def test_saved_popup_not_found_or_observed_does_not_prove_absence(completed):
    case = saved_case()
    capture_id = case["provenance"]["capture_id"]
    signature = case["detection"]["candidate_actions"][0]["obstruction_signature"]
    metadata = {
        "popup_absence_reobserve_completed": completed,
        "popup_absence_obstruction_signature": signature,
    }
    if not completed:
        metadata.update(
            action_reobserve_error_code="POPUP_DISMISSAL_NOT_FOUND",
            action_reobservation_retryable=True,
            h5_action_reobservation={
                "schema_version": "h5-action-reobservation.v1",
                "before_capture_id": capture_id,
            },
            action_reobservation_candidate_identity={
                "action_type": "click",
                "semantic_target": "关闭当前弹窗",
                "expected_next_page_type": "home",
            },
        )
    frontier = build_h5_frontier(
        detection_result=case["detection"],
        observed_at=datetime(2026, 10, 19, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata=metadata,
    )
    assert frontier.observation.page_state == "popup"
    assert all("自动退场" not in candidate.semantic_target for candidate in frontier.candidates)
    if completed:
        assert any(candidate.semantic_target == "关闭当前弹窗" for candidate in frontier.candidates)


def test_saved_timings_locate_delay_in_dom_not_screenshot_or_model():
    measured = [
        item["timings_ms"]
        for item in saved_case()["pipeline_timings"]
        if item["timings_ms"].get("dom", 0) > 300000
    ]
    assert len(measured) == 3
    assert all(item["screenshot"] < 200 and item["stable_wait"] < 16000 for item in measured)


@pytest.mark.parametrize("external", [False, True])
async def test_real_vant_ad_dialog_signed_candidate_is_executable_without_ad_navigation(external):
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    from services.api.app.agents.tools.page_detection_tools import (
        _OVERLAY_AD_CLOSE_SELECTOR,
        _with_obstruction_dismissal_candidate,
    )
    from services.api.app.infrastructure.browser.page_capture import (
        POPUP_OVERLAY_AD_CLOSE_SELECTOR,
        PageCaptureMode,
        PageCaptureRequest,
        PageTriggerAction,
        _collect_ui_snapshot,
        _run_single_trigger,
    )

    assert _OVERLAY_AD_CLOSE_SELECTOR == POPUP_OVERLAY_AD_CLOSE_SELECTOR
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route("**/*", lambda route: route.abort())
            await page.set_content("""<main>业务页</main><div id="scope">
              <div class="van-overlay" style="position:fixed;inset:0;z-index:2001"></div>
              <div class="van-popup van-popup--center"
                style="position:fixed;left:39px;top:210px;width:312px;height:425px;z-index:2002">
                <div class="ad-dialog"><div>推广弹窗</div><i class="van-icon van-icon-close"
                  style="position:absolute;left:133px;bottom:0;width:46px;height:46px">×</i>
                </div></div></div><script>
                document.querySelector('i').onclick=()=>document.querySelector('#scope').remove();
                </script>""")
            if external:
                await page.locator("i").evaluate(
                    "node=>node.dataset.url='https://outside.invalid/'"
                )
            snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:popup-case")
            actions = _with_obstruction_dismissal_candidate(
                page_type="home",
                page_state="popup",
                candidate_actions=[],
                dom_chunks=[await page.content()],
                ui_snapshot=snapshot,
            )
            if external:
                # 旧签发合同也必须在执行前重新拒绝危险目标，不能因为图标像关闭就放行。
                with pytest.raises(SemanticTargetResolutionError):
                    await UiSnapshotSemanticTargetResolver(
                        snapshot_reader=StaticSnapshotReader(snapshot)
                    ).resolve(request())
                assert await page.locator("#scope").count() == 1
                return
            assert actions
            action = actions[-1]
            from dataclasses import replace

            signed = replace(
                request(),
                parameters={
                    "target_mode": "popup_dismissal",
                    "popup_dismissal_capability": action.obstruction_capability,
                    "obstruction_signature": action.obstruction_signature,
                },
            )
            target = await UiSnapshotSemanticTargetResolver(
                snapshot_reader=StaticSnapshotReader(snapshot)
            ).resolve(signed)
            trace = []
            await _run_single_trigger(
                page=page,
                context=page.context,
                request=PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    session_id="h5:popup-case",
                    trigger_action=PageTriggerAction.CLICK,
                    selector=target.selector,
                    h5_popup_dismissal_strategy="safe_outside_then_close",
                    capture_screenshot=False,
                ),
                action_trace=trace,
                timeout_ms=3000,
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert await page.locator("#scope").count() == 0
            assert trace[-1]["dismissal_verified"] is True
            assert page.url == "about:blank"
        finally:
            await browser.close()
