"""本轮真实失败结构的最小复现；裁剪件不冒充完整现场或验收通过。"""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from services.api.app.application.h5_profile_payment_navigation import (
    H5_PROFILE_PAYMENT_VIP_TEXT_CAPABILITY,
    h5_profile_payment_snapshot_selector,
)
from services.api.app.application.observation_packet import ObservationArtifact, _decode_artifacts
from services.api.app.application.rule_capability_facts import snapshot_sequence
from services.api.app.application.rule_judge import RuleJudgeValidationError, _decode_role_evidence
from services.api.app.infrastructure.browser import page_capture
from services.api.app.infrastructure.rule_judge_evidence import (
    TrustedEvidenceArtifact,
    TrustedEvidenceBundle,
)

CASE = json.loads((Path(__file__).parent / "fixtures/h5_chain_20261012_103747.json").read_text())


@pytest.mark.parametrize("stage", ["serialization", "logical_surface"])
def test_yc_bound_snapshot_keeps_exact_payment_target(monkeypatch, stage):
    monkeypatch.setattr(page_capture, "MAX_UI_SNAPSHOT_BYTES", 4000)
    element = deepcopy(CASE["yc_profile"]["element"])
    snapshot = {
        "h5_modal_census_selectors": [],
        "h5_modal_census_truncated": False,
        "elements": [
            element,
            *[
                {
                    "selector": f"#ad-{index}",
                    "tag": "a",
                    "text": "广告栏目" * 200,
                    "interactive": True,
                }
                for index in range(20)
            ],
        ],
    }
    if stage == "serialization":
        snapshot = page_capture._sanitize_and_bound_ui_snapshot(snapshot)
    else:
        page_capture._bound_snapshot_after_logical_surface(snapshot)
    assert element["selector"] in {item["selector"] for item in snapshot["elements"]}
    assert page_capture._json_size_bytes(snapshot) <= 4000
    assert (
        h5_profile_payment_snapshot_selector(
            element, capability=H5_PROFILE_PAYMENT_VIP_TEXT_CAPABILITY
        )
        == element["selector"]
    )


@pytest.mark.parametrize("fault", ["covered", "submit", "wrong_parent", "aggregate"])
def test_payment_target_requires_actual_button_not_container_or_submit(fault):
    element = deepcopy(CASE["yc_profile"]["element"])
    if fault == "covered":
        element["hit_test"]["center_hits_target"] = False
    elif fault == "submit":
        element["text"] = "立即支付"
    elif fault == "wrong_parent":
        element["parent_class_tokens"] = ["promotion"]
    else:
        element["class_tokens"] = ["aspect-ratio-overlay"]
        element["text"] = "会员中心 新人VIP 立即开通"
    assert (
        h5_profile_payment_snapshot_selector(
            element, capability=H5_PROFILE_PAYMENT_VIP_TEXT_CAPABILITY
        )
        is None
    )


def _bundle(payloads):
    return TrustedEvidenceBundle(
        "packet",
        "capture",
        {"primary": "capture"},
        tuple(
            TrustedEvidenceArtifact(
                str(index),
                "ui_snapshot",
                "capture",
                "primary",
                "application/json",
                json.dumps(payload).encode(),
            )
            for index, payload in enumerate(payloads)
        ),
    )


def test_yc_baseline_and_scroll_sequence_are_not_duplicate_baselines():
    baseline = {"schema_version": "ui-snapshot.v2", "elements": []}
    sequence = CASE["yc_sequence"]["payload"]
    bundle = _bundle([baseline, sequence])
    result = _decode_role_evidence(bundle, artifact_types=("ui_snapshot",))
    assert result["primary"]["ui_snapshot"] == baseline
    assert result["primary"]["ui_snapshot_sequence"] == sequence
    assert bundle.require_unique("ui_snapshot").decode_json() == baseline


def test_conflicting_primary_snapshots_remain_invalid():
    with pytest.raises(RuleJudgeValidationError):
        _decode_role_evidence(
            _bundle(
                [
                    {"schema_version": "ui-snapshot.v2", "elements": []},
                    {"schema_version": "ui-snapshot.v2", "elements": [{"text": "其他页面"}]},
                ]
            ),
            artifact_types=("ui_snapshot",),
        )


@pytest.mark.parametrize("reverse", [False, True])
def test_bound_sequence_frames_still_reach_rule_packet(reverse):
    baseline = {"schema_version": "ui-snapshot.v2", "elements": [{"text": "基线"}]}
    sequence = {
        **CASE["yc_sequence"]["payload"],
        "frames": [
            {"index": 0, "elements": [{"text": "首项"}]},
            {"index": 1, "elements": [{"text": "末项"}]},
        ],
    }
    payloads = [baseline, sequence]
    if reverse:
        payloads.reverse()
    artifacts = [
        ObservationArtifact(
            str(index), "ui_snapshot", "application/json", json.dumps(value).encode()
        )
        for index, value in enumerate(payloads)
    ]
    facts = _decode_artifacts({"ui_snapshot": artifacts})
    assert [frame["index"] for frame in snapshot_sequence({"facts": facts})] == [0, 1]
    assert facts["ui_snapshot"]["sequence"] is True


@pytest.mark.parametrize("loaded", [False, True, "div_body", "media_pending"])
async def test_dense_navigation_does_not_promote_empty_loading_body(loaded):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
              <main><header>热门推荐 最新更新 社区 内容分类 内容详情 会员中心
              <nav><button>栏目一</button><button>栏目二</button><button>栏目三</button>
              <button>栏目四</button><button>栏目五</button></nav></header>
              <div class="spinner" style="width:20px;height:20px"></div></main>
            """)
            if loaded:
                content = (
                    "<div>真实加载的正文内容</div>"
                    if loaded == "div_body"
                    else "<article><p>真实加载的正文内容</p></article>"
                )
                await page.locator("main").evaluate(
                    "(node,content) => node.innerHTML += content", content
                )
            if loaded == "media_pending":
                await page.route("https://fixture.test/pending.png", lambda route: route.abort())
                await page.locator("main").evaluate("""node => {
                    const img = document.createElement('img'); img.width=100; img.height=100;
                    img.src='https://fixture.test/pending.png'; node.prepend(img);
                }""")
            artifacts = [
                page_capture.PageCaptureArtifact("dom", None, await page.content()),
                page_capture.PageCaptureArtifact(
                    "visible_text", None, await page.inner_text("body")
                ),
                page_capture.PageCaptureArtifact(
                    "ui_snapshot", None, {"elements": [{"text": str(i)} for i in range(20)]}
                ),
                page_capture.PageCaptureArtifact(
                    "screenshot", None, {"encoding": "base64", "data": "aW1hZ2U="}
                ),
            ]
            ready, diagnostics = await page_capture._assess_page_inspection_readiness(
                page=page,
                artifacts=artifacts,
            )
            assert ready is (bool(loaded) and loaded != "media_pending")
            if not loaded:
                assert "business_content_loading" in diagnostics["blocking_reasons"]
        finally:
            await browser.close()


@pytest.mark.parametrize("variant", ["bottom_ad_loading", "ready", "fullscreen"])
async def test_ad_close_does_not_make_loading_body_ready(variant):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
              <main style="height:600px"><h1>热门推荐 最新更新 社区 内容分类</h1>
              <div class="loading">加载中...</div></main>
              <div class="ad loading-page" style="position:fixed;bottom:0;width:300px;height:40px">
              <button class="down-time">关闭</button></div>
            """)
            if variant == "ready":
                await page.locator(".loading").evaluate("node => node.remove()")
                await page.locator("main").evaluate("node => node.innerHTML += '<p>已加载正文</p>'")
            elif variant == "fullscreen":
                await page.locator(".ad").evaluate(
                    "node => node.style.cssText='position:fixed;inset:0;background:white'"
                )
            diagnostics = {}
            ready = await page_capture._wait_for_page_settle(
                page=page,
                max_wait_ms=900,
                poll_interval_ms=50,
                stable_samples=2,
                diagnostics=diagnostics,
            )
            assert ready is (variant != "bottom_ad_loading")
            if variant == "ready":
                assert diagnostics["outcome"] == "settled"
            if variant == "fullscreen":
                assert diagnostics["outcome"] == "startup_obstruction_ready"
        finally:
            await browser.close()
