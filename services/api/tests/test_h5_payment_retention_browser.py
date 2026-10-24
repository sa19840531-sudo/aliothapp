"""YC 按钮的真实 DOM 采集与有界快照，不访问业务网站或提交支付。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from services.api.app.application.h5_profile_payment_navigation import (
    H5_PROFILE_PAYMENT_TARGET,
    H5_PROFILE_PAYMENT_VIP_TEXT_CAPABILITY,
    h5_profile_payment_snapshot_selector,
)
from services.api.app.application.interaction_runtime import (
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
)
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.browser import page_capture
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)


@pytest.mark.parametrize("covered", [True, False])
async def test_actual_payment_dom_is_retained_but_covered_button_is_not_authorized(
    monkeypatch, covered
):
    monkeypatch.setattr(page_capture, "MAX_UI_SNAPSHOT_BYTES", 16000)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route("**/*", lambda route: route.abort())
            await page.set_content("""
                <style>.item{position:absolute;top:100px;left:0;width:350px;height:24px;overflow:hidden}
                .button{position:absolute;top:20px;left:20px;width:100px;height:40px}
                #cover{position:absolute;inset:0 0 auto;height:80px;background:white;z-index:100}
                </style>
                <header class="header"><div class="vip-section">
                <div class="button" id="payment">立即开通</div></div></header>
                <main id="crowded"></main>
            """)
            await page.evaluate("""() => {
                for(let i=0;i<150;i++){
                    const a=document.createElement('a'); a.className='item'; a.id='item'+i;
                    a.href='#'; a.textContent='业务目录'.repeat(100);
                    document.querySelector('#crowded').append(a);
                }
            }""")
            if covered:
                await page.evaluate(
                    "document.body.insertAdjacentHTML('beforeend','<div id=cover></div>')"
                )
            snapshot = await page_capture._collect_ui_snapshot(
                page, surface_session_id="h5:fixture"
            )
            matches = [
                element for element in snapshot["elements"] if element.get("selector") == "#payment"
            ]
            assert len(matches) == 1
            assert page_capture._json_size_bytes(snapshot) <= 16000
            assert bool(
                h5_profile_payment_snapshot_selector(
                    matches[0],
                    capability=H5_PROFILE_PAYMENT_VIP_TEXT_CAPABILITY,
                )
            ) is (not covered)
            assert snapshot.get("truncated") is True
            resolver = UiSnapshotSemanticTargetResolver(
                snapshot_reader=SimpleNamespace(
                    read_ui_snapshot=AsyncMock(return_value=snapshot),
                )
            )
            request = SemanticTargetResolutionRequest(
                task_id="task",
                session_id="h5:fixture",
                source_capture_id="fixture-capture",
                action_type=InteractionOperation.CLICK,
                semantic_target=H5_PROFILE_PAYMENT_TARGET,
                parameters={
                    "target_mode": "profile_payment_navigation",
                    "payment_entry_capability": H5_PROFILE_PAYMENT_VIP_TEXT_CAPABILITY,
                    "source_page_type": "profile",
                    "expected_page_type": "payment",
                },
            )
            if covered:
                with pytest.raises(SemanticTargetResolutionError) as error:
                    await resolver.resolve(request)
                assert error.value.error_code == "PROFILE_PAYMENT_TARGET_NOT_FOUND"
            else:
                assert (await resolver.resolve(request)).selector == "#payment"
        finally:
            await browser.close()
