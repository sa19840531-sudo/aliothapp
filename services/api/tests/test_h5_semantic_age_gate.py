"""年龄门禁依赖同帧语义及父层证明，不依赖产品类名。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from services.api.app.application.h5_age_verification import (
    h5_snapshot_age_verification_confirm_selectors,
)


def _snapshot():
    modal = {
        "selector": "#dialog", "role": "dialog", "tag": "flt-semantics",
        "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
    }
    controls = [
        {"selector": selector, "text": text, "tag": "flt-semantics",
         "interactive": True, "h5_modal_ancestor_selectors": ["#dialog"],
         "rect": {"x": 44, "y": y, "width": 302, "height": 52},
         "hit_test": {"center_hits_target": True}}
        for selector, text, y in [
            ("#leave", "我未满18岁-离开", 430),
            ("#enter", "我年满18岁-进入", 494),
        ]
    ]
    return {"h5_modal_census_truncated": False,
            "h5_modal_census_selectors": ["#dialog", "#leave", "#enter"],
            "viewport": {"width": 390, "height": 844}, "elements": [modal, *controls]}


@pytest.mark.parametrize("case,expected", [
    ("paired", ("#enter",)), ("missing_leave", ()), ("duplicate", ()),
    ("different_layer", ()), ("external", ()), ("not_hittable", ()),
    ("truncated", ()), ("not_h5", ()), ("disabled", ()),
    ("hidden", ()), ("form", ()), ("ordinary_page", ()),
])
def test_generic_age_gate_requires_complete_unique_local_pair(case, expected):
    snapshot = _snapshot()
    if case == "missing_leave":
        snapshot["elements"].pop(1)
    elif case == "duplicate":
        snapshot["elements"].append({**deepcopy(snapshot["elements"][-1]),
                                     "selector": "#other-enter"})
    elif case == "different_layer":
        snapshot["elements"][1]["h5_modal_ancestor_selectors"] = ["#other-dialog"]
    elif case == "external":
        snapshot["elements"][-1]["href"] = "https://outside.test/"
    elif case == "not_hittable":
        snapshot["elements"][-1]["hit_test"]["center_hits_target"] = False
    elif case == "truncated":
        snapshot["truncated"] = True
    elif case == "not_h5":
        snapshot.pop("h5_modal_census_truncated")
    elif case == "disabled":
        snapshot["elements"][-1]["disabled"] = True
    elif case == "hidden":
        snapshot["elements"][-1]["style"] = {"display": "none"}
    elif case == "form":
        snapshot["elements"].append({"tag": "input", "interactive": True,
                                    "h5_modal_ancestor_selectors": ["#dialog"]})
    elif case == "ordinary_page":
        snapshot["elements"][0]["role"] = None
    assert h5_snapshot_age_verification_confirm_selectors(snapshot) == expected


def test_semantic_age_gate_is_detected_before_business_rules_are_enabled():
    from services.api.app.agents.tools.page_detection_tools import (
        _deterministic_age_verification_result,
    )

    result = _deterministic_age_verification_result(
        dom_chunks=["<main>匿名产品</main>"], ui_snapshot=_snapshot(),
        base_values={"page_capture_id": "capture-age", "catalog_version": "test", "model": "test"},
    )
    assert result["page_state"] == "age_verification_blocking"
    assert result["rule_matching_allowed"] is False
    assert (
        result["candidate_actions"][0]["obstruction_capability"]
        == "h5_age_verification_confirm.v1"
    )


@pytest.mark.asyncio
async def test_semantic_age_gate_from_real_browser_resolves_and_enters_same_page():
    from services.api.app.application.interaction_runtime import SemanticTargetResolutionRequest
    from services.api.app.domain.interaction import InteractionOperation
    from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
    from services.api.app.infrastructure.browser.semantic_target_resolver import (
        UiSnapshotSemanticTargetResolver,
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <style>body{margin:0} #gate{position:fixed;inset:0;background:white;
                z-index:999} flt-semantics[flt-tappable]{position:absolute;left:44px;
                width:302px;height:52px;cursor:pointer} #leave{top:430px} #enter{top:494px}</style>
                <main><h1>产品首页</h1></main>
                <flt-semantics id="gate" role="dialog" aria-modal="true">
                    <flt-semantics id="leave" flt-tappable
                        onclick="document.querySelector('main').textContent='已离开'">
                        我未满18岁-离开</flt-semantics>
                    <flt-semantics id="enter" flt-tappable
                        onclick="document.querySelector('#gate').remove()">我年满18岁-进入
                    </flt-semantics>
                </flt-semantics>
            """)
            snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:age-fixture")
            assert h5_snapshot_age_verification_confirm_selectors(snapshot) == ("#enter",)
            resolver = UiSnapshotSemanticTargetResolver(
                snapshot_reader=SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=snapshot)),
            )
            target = await resolver.resolve(SemanticTargetResolutionRequest(
                task_id="age-fixture", session_id="h5:age-fixture", source_capture_id="before",
                action_type=InteractionOperation.CLICK, semantic_target="年满进入",
                parameters={"target_mode": "age_verification_confirm",
                            "age_verification_capability": "h5_age_verification_confirm.v1"},
            ))
            await page.locator(target.selector).click()
            after = await _collect_ui_snapshot(page, surface_session_id="h5:age-fixture")
            assert after["h5_modal_census_selectors"] == []
            assert await page.locator("#gate").count() == 0
            assert await page.locator("main").inner_text() == "产品首页"
        finally:
            await browser.close()
