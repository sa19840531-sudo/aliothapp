"""Web 非广告主体目标与有界补图测试。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.web_interaction_facts import web_capture_content_facts
from services.api.app.application.web_page_classification import web_page_structure_facts
from services.api.app.application.web_page_readiness import WebObstructionAssessment
from services.api.app.application.web_primary_content_capture import (
    WebPrimaryContentCaptureService,
    _post_scroll_block_reason,
    build_web_primary_content_targets,
    resolve_web_primary_content_selector,
    viewport_ad_coverage,
)
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.tests.web_browser_test_support import (
    WebBrowserUnavailable,
    launch_web_test_browser,
)


class _ReadyEvidence:
    mode = "stable"

    async def read_ready(self, *, capture_id, page_role):
        facts = web_capture_content_facts(
            await self.read_snapshot(capture_id=capture_id),
            url=await self.read_url(capture_id=capture_id),
        )
        return SimpleNamespace(
            interaction_facts=replace(
                facts,
                identity_verified=self.mode != "old_capture",
                readiness_accepted=self.mode != "loading",
            )
        )


def _element(selector: str, **overrides):
    item = {
        "selector": selector,
        "tag": "article",
        "visible": True,
        "class_tokens": ["content-card"],
        "ancestor_class_tokens": ["content-grid"],
        "is_repeated_card": True,
        "repeated_group_fingerprint": "grid-1",
        "dom_fingerprint": selector,
        "rect": {"x": 0, "y": 200, "width": 600, "height": 300},
    }
    item.update(overrides)
    return item


@pytest.mark.asyncio
async def test_primary_content_reuses_trusted_viewport_when_target_is_already_exposed() -> None:
    class Evidence(_ReadyEvidence):
        async def read_snapshot(self, *, capture_id):
            assert capture_id == "capture-ready"
            return {
                "viewport": {"width": 1000, "height": 800},
                "elements": [
                    _element(
                        "#visible-card",
                        rect={"x": 100, "y": 120, "width": 600, "height": 300},
                    ),
                    _element(
                        "#small-float",
                        is_repeated_card=False,
                        is_advertisement=True,
                        rect={"x": 920, "y": 700, "width": 60, "height": 60},
                    ),
                ],
            }

        async def read_url(self, *, capture_id):
            assert capture_id == "capture-ready"
            return "https://example.com/"

    class Coordinator:
        async def coordinate(self, _command):
            raise AssertionError("首屏主体已经暴露时不得产生无效滚动")

    result = await WebPrimaryContentCaptureService(
        coordinator=Coordinator(),
        evidence_source=Evidence(),
    ).capture(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="capture-ready",
        page_url="https://example.com/",
        page_type="home",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )

    assert result.status == "captured"
    assert result.evidence_capture_id == "capture-ready"
    assert result.scroll_count == 0
    assert result.reused is True


def test_each_primary_target_keeps_its_own_selector() -> None:
    targets = build_web_primary_content_targets(
        capture_id="capture-1",
        expected_page_type="content_detail",
        ui_snapshot={
            "viewport": {"width": 1000, "height": 800},
            "elements": [
                _element("#a", is_repeated_card=False, contains_primary_media=True),
                _element("#b", is_repeated_card=False),
            ],
        },
    )
    assert {item.dom_fingerprint: item.selector for item in targets} == {"#a": "#a", "#b": "#b"}


@pytest.mark.parametrize("page_type", ["home", "content_list", "video_list"])
def test_real_card_children_cannot_form_a_repeated_group_across_different_node_kinds(page_type):
    # 真实PG/Chrome失败快照的最小结构：同一卡片内的链接和正文不能互算两张重复卡。
    rows = []
    for index in (1, 2):
        card = f"#body > section > article:nth-of-type({index})"
        for selector, tag, parent in ((card, "article", "#body > section"),
                                      (card + " > a", "a", card),
                                      (card + " > p", "p", card)):
            rows.append({"selector": selector, "tag": tag, "parent_selector": parent,
                         "visible": True, "rect": {"x": 0, "y": 1400, "width": 1920,
                                                    "height": 900 if tag == "article" else 22}})
    targets = build_web_primary_content_targets(
        capture_id="actual-failed-baseline", expected_page_type=page_type,
        ui_snapshot={"elements": rows},
    )
    assert {t.selector for t in targets} == {
        "#body > section > article:nth-of-type(1)",
        "#body > section > article:nth-of-type(2)",
    }


@pytest.mark.parametrize("navigation", [False, True])
def test_same_kind_business_links_keep_their_existing_repeated_group_contract(navigation):
    targets = build_web_primary_content_targets(
        capture_id="counterexample", expected_page_type="home",
        ui_snapshot={"elements": [
            {"selector": f"#group > a:nth-child({i})", "tag": "a", "visible": True,
             "parent_selector": "#group", "within_web_navigation": navigation}
            for i in (1, 2)
        ]},
    )
    assert len(targets) == (0 if navigation else 2)


@pytest.mark.parametrize(
    "page_type,element,want",
    [
        ("video_detail", {"tag": "div", "class_tokens": ["player-root"]}, False),
        ("video_detail", {"tag": "video"}, False),
        ("video_detail", {"tag": "video", "src": "/film.mp4"}, True),
        ("video_detail", {"tag": "source", "media_parent_tag": "video", "src": "/film.mp4"}, True),
        ("content_detail", {"tag": "div", "class_tokens": ["image-gallery"]}, True),
        ("activity", {"tag": "main", "class_tokens": ["campaign"]}, True),
        ("search_page", {"tag": "form", "role": "search"}, True),
    ],
)
def test_v8_primary_targets_require_resource_and_support_new_subjects(page_type, element, want):
    targets = build_web_primary_content_targets(
        capture_id="capture-1",
        expected_page_type=page_type,
        ui_snapshot={"elements": [{"visible": True, "selector": "#subject", **element}]},
    )
    assert bool(targets) is want


@pytest.mark.parametrize(
    "flag", ["within_footer", "within_breadcrumb", "within_pagination", "within_web_navigation"]
)
def test_primary_target_rejects_structural_nonbusiness_regions(flag) -> None:
    targets = build_web_primary_content_targets(
        capture_id="capture-1",
        expected_page_type="content_detail",
        ui_snapshot={"elements": [_element("#neutral", **{flag: True})]},
    )
    assert targets == ()


@pytest.mark.parametrize(
    "snapshot,want",
    [
        (
            {
                "viewport": {"width": 1000, "height": 800},
                "elements": [
                    _element(
                        "#ad",
                        is_advertisement=True,
                        rect={"x": 0, "y": -1000, "width": 1000, "height": 800},
                    )
                ],
            },
            0.0,
        ),
        (
            {
                "viewport": {"width": 1000, "height": 800},
                "elements": [
                    _element(
                        "#ad-a",
                        is_advertisement=True,
                        rect={"x": 0, "y": 0, "width": 1000, "height": 200},
                    ),
                    _element(
                        "#ad-b",
                        is_advertisement=True,
                        rect={"x": 0, "y": 0, "width": 1000, "height": 200},
                    ),
                ],
            },
            0.25,
        ),
        ({"elements": []}, None),
        (
            {
                "viewport": {"width": 1000, "height": 800},
                "elements": [_element("#ad", is_advertisement=True, rect=None)],
            },
            None,
        ),
        (
            {
                "viewport_ad_coverage": 0.0,
                "viewport": {"width": 1000, "height": 800},
                "elements": [
                    _element(
                        "#ad",
                        is_advertisement=True,
                        rect={"x": 0, "y": 0, "width": 1000, "height": 800},
                    )
                ],
            },
            1.0,
        ),
        (
            {
                "viewport": {
                    "width": 1000,
                    "height": 800,
                    "scroll_y": 1000,
                    "device_pixel_ratio": 2,
                },
                "elements": [
                    _element(
                        "#ad",
                        is_advertisement=True,
                        rect={"x": 0, "y": 0, "width": 1000, "height": 200},
                    )
                ],
            },
            0.25,
        ),
    ],
)
def test_advertisement_coverage_uses_css_viewport_union_and_unknown(snapshot, want) -> None:
    assert viewport_ad_coverage(snapshot) == want


@pytest.mark.parametrize(
    "rect,visible,want",
    [
        ({"x": -500, "y": 0, "width": 1000, "height": 200}, True, 0.125),
        (None, False, 0.0),
        ({"x": float("nan"), "y": 0, "width": 1000, "height": 200}, True, None),
        ({"x": 0, "y": 0, "width": -1, "height": 200}, True, None),
    ],
)
def test_advertisement_geometry_partial_hidden_and_invalid(rect, visible, want):
    assert (
        viewport_ad_coverage(
            {
                "viewport": {"width": 1000, "height": 800},
                "elements": [_element("#ad", is_advertisement=True, visible=visible, rect=rect)],
            }
        )
        == want
    )


def test_advertisement_geometry_uses_visual_viewport_offset():
    snapshot = {
        "viewport": {
            "width": 1000,
            "height": 800,
            "visual_viewport_width": 500,
            "visual_viewport_height": 400,
            "visual_viewport_offset_left": 100,
            "visual_viewport_offset_top": 50,
        },
        "elements": [
            _element(
                "#ad", is_advertisement=True, rect={"x": 0, "y": 0, "width": 200, "height": 150}
            )
        ],
    }
    assert viewport_ad_coverage(snapshot) == 0.05
    del snapshot["viewport"]["visual_viewport_offset_top"]
    assert viewport_ad_coverage(snapshot) is None


@pytest.mark.parametrize("coverage", (0.001, 0.05, 0.299999))
def test_ordinary_local_overlay_below_subject_threshold_does_not_block_primary_content_scroll(
    coverage: float,
) -> None:
    elements = [
        _element(
            "main",
            tag="main",
            is_repeated_card=False,
            rect={"x": 100, "y": 100, "width": 800, "height": 600},
        ),
        _element(
            "#ordinary-overlay",
            tag="div",
            is_repeated_card=False,
            web_obstruction_layer_structural=True,
            web_obstruction_layer="#ordinary-overlay",
            web_obstruction_layer_kind="geometric_overlay",
            fixed_or_sticky_ancestor=False,
            style={"position": "absolute"},
            hit_test={"visual_occlusion_detected": False},
            rect={
                "x": 100,
                "y": 100,
                "width": 800 * coverage,
                "height": 600,
            },
        ),
    ]

    assert (
        _post_scroll_block_reason(
            {
                "url": "https://card-layout.test/list",
                "viewport": {"width": 1000, "height": 800},
                "elements": elements,
            }
        )
        is None
    )


@pytest.mark.parametrize(
    "obstruction",
    (
        {
            "web_obstruction_layer_kind": "modal",
            "within_repeated_structure": False,
            "repeat_group_size": 1,
            "viewport_coverage": 0.4,
            "fixed_or_sticky_ancestor": True,
            "style": {"position": "fixed"},
            "hit_test": {"visual_occlusion_detected": True},
        },
        {
            "web_obstruction_layer_kind": "dialog",
            "within_repeated_structure": True,
            "repeat_group_size": 25,
            "viewport_coverage": 0.0128,
            "fixed_or_sticky_ancestor": False,
            "style": {"position": "absolute"},
            "hit_test": {"visual_occlusion_detected": True},
        },
        {
            "web_obstruction_layer_kind": "modal",
            "within_repeated_structure": True,
            "repeat_group_size": 25,
            "viewport_coverage": 0.3,
            "fixed_or_sticky_ancestor": False,
            "style": {"position": "absolute"},
            "hit_test": {"visual_occlusion_detected": False},
        },
    ),
)
def test_true_modal_or_visual_obstruction_still_blocks_primary_content_scroll(
    obstruction,
) -> None:
    assert (
        _post_scroll_block_reason(
            {
                "url": "https://modal-layout.test/list",
                "elements": [
                    _element(
                        "#obstruction",
                        web_obstruction_layer_structural=True,
                        **obstruction,
                    )
                ],
            }
        )
        == "WEB_PRIMARY_CONTENT_OBSTRUCTION_APPEARED"
    )


@pytest.mark.asyncio
async def test_real_chromium_distinguishes_repeated_local_overlays_from_modal() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    cards = "".join(
        f'<article class="content-card"><span>card {index}</span>'
        '<div class="overlay card-local-layer"></div></article>'
        for index in range(25)
    )
    html = f"""
    <style>
      html, body {{ margin: 0; }}
      main {{ display: grid; grid-template-columns: repeat(5, 180px); gap: 8px; }}
      .content-card {{ position: relative; width: 180px; height: 100px; }}
      .card-local-layer {{ position: absolute; right: 2px; top: 2px;
        width: 24px; height: 24px; background: #ddd; }}
      .actual-modal {{ position: fixed; left: 250px; top: 160px;
        width: 500px; height: 360px; background: white; z-index: 100; }}
    </style>
    <main>{cards}</main>
    """
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1000, "height": 800})
            await page.set_content(html)
            card_snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await page.locator("body").evaluate(
                "body => body.insertAdjacentHTML('beforeend', "
                '\'<div class="actual-modal" role="dialog">modal</div>\')'
            )
            modal_snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    local_layers = [
        item
        for item in card_snapshot["elements"]
        if "card-local-layer" in item.get("class_tokens", [])
    ]
    assert len(local_layers) == 25
    assert all(item["web_obstruction_layer_structural"] is True for item in local_layers)
    assert all(item["within_repeated_structure"] is True for item in local_layers)
    assert all(item["repeat_group_size"] == 25 for item in local_layers)
    assert _post_scroll_block_reason(card_snapshot) is None
    assert _post_scroll_block_reason(modal_snapshot) == ("WEB_PRIMARY_CONTENT_OBSTRUCTION_APPEARED")


@pytest.mark.asyncio
async def test_dense_navigation_cannot_evict_detail_subject_from_web_snapshot() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    links = "".join(f'<a href="/category/{index}">栏目 {index}</a>' for index in range(260))
    html = (
        f'<nav class="navbar"><div class="bottom-nav">{links}</div></nav>'
        '<main id="post" role="main"><article class="post">'
        '<h1 class="post-title">详情标题</h1>'
        '<div class="nav-breadcrumb-wrap"><a href="/">首页</a><span>详情</span></div>'
        '<div class="post-content"><p>'
        f'{"这是足够明确的详情正文内容，用于通用主体识别。" * 20}'
        '</p></div>'
        '</article></main>'
    )
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    elements = snapshot["elements"]
    selectors = {str(item.get("selector") or "") for item in elements}
    assert "#post" in selectors
    assert any("post-content" in item.get("class_tokens", []) for item in elements)
    assert any("post-title" in item.get("class_tokens", []) for item in elements)
    facts = web_page_structure_facts(snapshot)
    assert facts.single_subject_element_count >= 2
    assert facts.article_body_count >= 2
    assert facts.paragraph_text_length >= 200


def test_list_target_requires_repeated_non_ad_business_cards() -> None:
    targets = build_web_primary_content_targets(
        capture_id="capture-1",
        expected_page_type="content_list",
        ui_snapshot={
            "viewport": {"width": 1000, "height": 800},
            "elements": [
                _element("#card-1"),
                _element("#card-2"),
                _element(
                    "#ad",
                    is_advertisement=True,
                    rect={"x": 0, "y": 0, "width": 1000, "height": 496},
                ),
                _element("#footer", class_tokens=["footer"], is_repeated_card=False),
                _element("#pager", class_tokens=["pagination"], is_repeated_card=False),
            ],
        },
    )

    assert len(targets) == 2
    assert all(target.viewport_ad_coverage == 0.62 for target in targets)
    assert {target.dom_fingerprint for target in targets} == {"#card-1", "#card-2"}


def test_list_target_ignores_legacy_body_nav_pollution_for_web_content() -> None:
    article = _element(
        "#feed > article:nth-of-type(1)",
        tag="article",
        is_repeated_card=False,
        within_navigation=True,
        within_web_navigation=False,
        parent_selector="#feed",
    )
    article["within_repeated_structure"] = True
    peer = dict(article)
    peer["selector"] = "#feed > article:nth-of-type(2)"

    targets = build_web_primary_content_targets(
        capture_id="capture-1",
        expected_page_type="home",
        ui_snapshot={"elements": [article, peer]},
    )

    assert {target.selector for target in targets} == {
        "#feed > article:nth-of-type(1)",
        "#feed > article:nth-of-type(2)",
    }


def test_list_target_still_rejects_proven_web_navigation() -> None:
    navigation_item = _element(
        "#top-nav > article:nth-of-type(1)",
        tag="article",
        is_repeated_card=True,
        within_navigation=True,
        within_web_navigation=True,
    )

    targets = build_web_primary_content_targets(
        capture_id="capture-1",
        expected_page_type="home",
        ui_snapshot={"elements": [navigation_item]},
    )

    assert targets == ()


@pytest.mark.parametrize(
    "page_type,element",
    (
        ("content_detail", _element("#body", tag="article", is_repeated_card=False)),
        (
            "video_detail",
            _element(
                "#player",
                tag="video",
                src="/primary.mp4",
                class_tokens=["main-player"],
                is_repeated_card=False,
            ),
        ),
    ),
)
def test_detail_target_selects_article_or_main_player(page_type, element) -> None:
    targets = build_web_primary_content_targets(
        capture_id="capture-1",
        expected_page_type=page_type,
        ui_snapshot={"elements": [element]},
    )

    assert len(targets) == 1


def test_primary_content_candidate_identity_survives_before_action_capture_refresh() -> None:
    snapshot = {"elements": [_element("#body", is_repeated_card=False)]}
    discovery_target = build_web_primary_content_targets(
        capture_id="capture-discovery",
        expected_page_type="content_detail",
        ui_snapshot=snapshot,
    )[0]
    before_action_target = build_web_primary_content_targets(
        capture_id="capture-before-action",
        expected_page_type="content_detail",
        ui_snapshot=snapshot,
    )[0]

    assert before_action_target.candidate_id == discovery_target.candidate_id
    assert before_action_target.dom_fingerprint == discovery_target.dom_fingerprint


@pytest.mark.parametrize(
    "change", ["ancestor-state", "target-tag", "target-class", "advertisement"]
)
def test_primary_target_rebind_ignores_ancestor_ui_state_but_checks_target(change) -> None:
    before = _element("#grid > article:nth-of-type(2)", dom_fingerprint=None)
    target = build_web_primary_content_targets(
        capture_id="before", ui_snapshot={"elements": [before]}, expected_page_type="content_list"
    )[0]
    after = dict(before)
    if change == "ancestor-state":
        after["ancestor_class_tokens"] = ["content-grid", "show-back-to-top"]
    elif change == "target-tag":
        after["tag"] = "section"
    elif change == "target-class":
        after["class_tokens"] = ["different-card"]
    else:
        after["is_advertisement"] = True
    resolved = resolve_web_primary_content_selector(
        capture_id="after",
        ui_snapshot={"elements": [after]},
        expected_page_type="content_list",
        candidate_id=target.candidate_id,
    )
    if change == "ancestor-state":
        assert resolved is not None
        assert resolved[0] == "#grid > article:nth-of-type(2)"
    else:
        assert resolved is None


def test_primary_content_selector_resolution_rejects_same_fingerprint_different_selector() -> None:
    snapshot = {
        "elements": [
            _element("#first", dom_fingerprint="shared-dom", is_repeated_card=False),
            _element("#second", dom_fingerprint="shared-dom", is_repeated_card=False),
        ]
    }
    second_target = next(
        target
        for target in build_web_primary_content_targets(
            capture_id="capture-1",
            expected_page_type="content_detail",
            ui_snapshot=snapshot,
        )
        if target.selector == "#second"
    )

    resolved = resolve_web_primary_content_selector(
        capture_id="capture-1",
        ui_snapshot=snapshot,
        expected_page_type="content_detail",
        candidate_id=second_target.candidate_id,
    )

    assert resolved is not None
    assert resolved[0] == "#second"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_type", "body_height"),
    [("content_detail", 300), ("home", 300), ("content_detail", 10806.75)],
)
@pytest.mark.parametrize(
    "scenario",
    [
        "stable",
        "loading",
        "old_capture",
        "forged_parent",
        "target_drift",
        "popup",
        "read_ready_error",
    ],
)
async def test_primary_content_capture_keeps_baseline_and_marks_supplement_version(
    scenario,
    page_type,
    body_height,
) -> None:
    snapshots = {
        "capture-ready": {
            "elements": [
                _element("#body", is_repeated_card=False),
                _element(
                    "#ad",
                    is_advertisement=True,
                    rect={"x": 0, "y": 0, "width": 1920, "height": 756},
                ),
            ],
        },
        "capture-content": {
            "elements": [
                _element("#body", is_repeated_card=False),
                _element(
                    "#ad",
                    is_advertisement=True,
                    rect={"x": 0, "y": 0, "width": 1920, "height": 108},
                ),
            ],
        },
    }
    if body_height > 1080:
        for snapshot in snapshots.values():
            snapshot["elements"][0]["rect"] = {
                "x": 630, "y": -0.2, "width": 660, "height": body_height,
            }
    if page_type == "home":
        for snapshot in snapshots.values():
            snapshot["elements"][0]["is_repeated_card"] = True
            snapshot["elements"].append(_element("#second-card"))
    if scenario == "target_drift":
        snapshots["capture-content"]["elements"][0]["dom_fingerprint"] = (
            "different-business-subject"
        )
        if page_type == "home":
            snapshots["capture-content"]["elements"][-1]["dom_fingerprint"] = (
                "different-second-card"
            )
    if scenario == "popup":
        snapshots["capture-content"]["elements"].append(
            _element("#gate", web_obstruction_layer_structural=True)
        )

    class Evidence(_ReadyEvidence):
        mode = scenario

        async def read_ready(self, *, capture_id, page_role):
            if scenario == "read_ready_error":
                raise RuntimeError("受控 ready 证据不可用")
            return await super().read_ready(capture_id=capture_id, page_role=page_role)

        async def read_snapshot(self, *, capture_id):
            return {"viewport": {"width": 1920, "height": 1080}, **snapshots[capture_id]}

        async def read_url(self, *, capture_id):
            assert capture_id == "capture-content"
            return "https://example.com/news?id=42#body"

    class Coordinator:
        def __init__(self):
            self.command = None

        async def coordinate(self, command):
            self.command = command
            return SimpleNamespace(
                before_capture=SimpleNamespace(
                    capture_id="fresh-before",
                    parent_capture_id="capture-ready",
                    action_execution_id="scroll-1",
                ),
                after_capture=SimpleNamespace(
                    capture_id="capture-content",
                    parent_capture_id="capture-ready"
                    if scenario == "forged_parent"
                    else "fresh-before",
                    action_execution_id="scroll-1",
                ),
                interaction=SimpleNamespace(execution_outcome="succeeded", status="succeeded"),
            )

    class Writer:
        def __init__(self):
            self.call = None

        async def mark_primary_content_capture(self, **kwargs):
            self.call = kwargs

    coordinator = Coordinator()
    writer = Writer()
    result = await WebPrimaryContentCaptureService(
        coordinator=coordinator,
        evidence_source=Evidence(),
        metadata_writer=writer,
    ).capture(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="capture-ready",
        page_url="https://EXAMPLE.com/news?id=42&utm_source=feed",
        page_type=page_type,
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )

    if scenario != "stable":
        assert result.status == "blocked"
        assert result.evidence_capture_id is None
        assert len(result.interaction_outcomes) == 1
        assert result.interaction_outcomes[0].command_accepted is True
        assert result.interaction_outcomes[0].transition_arrived is False
        assert writer.call is None
        return
    assert result.status == "captured"
    assert len(result.interaction_outcomes) == 1
    assert result.interaction_outcomes[0].transition_arrived is True
    assert result.parent_capture_id == "capture-ready"
    assert result.evidence_capture_id == "capture-content"
    assert coordinator.command.intent.parameters["capture_purpose"] == (
        "web_primary_content_evidence"
    )
    assert (
        coordinator.command.intent.parameters["web_primary_content_discovery_capture_id"]
        == "capture-ready"
    )
    assert "selector" not in coordinator.command.intent.parameters
    assert writer.call["parent_capture_id"] == "capture-ready"
    assert writer.call["direct_parent_capture_id"] == "fresh-before"
    assert (
        coordinator.command.intent.parameters["web_capture_stage"] == "web_primary_content_evidence"
    )
    assert writer.call["capture_purpose"] == "web_primary_content_evidence"


@pytest.mark.asyncio
async def test_primary_content_capture_stops_when_scroll_reveals_human_verification() -> None:
    snapshots = {
        "capture-ready": {
            "viewport_ad_coverage": 0.7,
            "elements": [_element("#body", is_repeated_card=False)],
        },
        "capture-after": {
            "viewport_ad_coverage": 0.1,
            "elements": [
                _element("#body", is_repeated_card=False),
                _element(
                    "#captcha",
                    is_repeated_card=False,
                    web_human_verification_present=True,
                ),
            ],
        },
    }

    class Evidence(_ReadyEvidence):
        async def read_snapshot(self, *, capture_id):
            return snapshots[capture_id]

        async def read_url(self, *, capture_id):
            return "https://example.com/article/42"

    class Coordinator:
        async def coordinate(self, _command):
            return SimpleNamespace(
                interaction=SimpleNamespace(execution_outcome="succeeded", status="succeeded"),
                before_capture=SimpleNamespace(
                    capture_id="fresh-before",
                    parent_capture_id="capture-ready",
                    action_execution_id="scroll-1",
                ),
                after_capture=SimpleNamespace(
                    capture_id="capture-after",
                    parent_capture_id="fresh-before",
                    action_execution_id="scroll-1",
                ),
            )

    result = await WebPrimaryContentCaptureService(
        coordinator=Coordinator(),
        evidence_source=Evidence(),
    ).capture(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="capture-ready",
        page_url="https://example.com/article/42",
        page_type="content_detail",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )

    assert result.status == "blocked"
    assert result.evidence_capture_id is None
    assert result.reason_code == "WEB_PRIMARY_CONTENT_HUMAN_VERIFICATION_BLOCKED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "subject_overlap",
        "obstruction_present",
        "advertisement_overlay",
        "expected_status",
        "expected_calls",
    ),
    (
        (0.299999, True, False, "captured", 0),
        (0.3, False, False, "captured", 1),
        (0.3, True, False, "blocked", 1),
        (0.299999, True, True, "captured", 0),
        (0.3, False, True, "captured", 1),
        (1.0, True, True, "blocked", 1),
    ),
)
async def test_primary_content_ordinary_overlay_hard_gate_controls_existing_model_assessor(
    subject_overlap: float,
    obstruction_present: bool,
    advertisement_overlay: bool,
    expected_status: str,
    expected_calls: int,
) -> None:
    source_url = "https://overlay-threshold.test/article/42"

    def snapshot(*, after: bool) -> dict[str, object]:
        subject = _element(
            "main",
            tag="main",
            is_repeated_card=False,
            dom_fingerprint="stable-main",
            rect={"x": 100, "y": 0 if after else 100, "width": 800, "height": 600},
        )
        if not after:
            return {
                "viewport": {"width": 1000, "height": 800},
                "elements": [subject],
            }
        return {
            "viewport": {"width": 1000, "height": 800},
            "elements": [
                subject,
                _element(
                    "#ordinary-overlay",
                    tag="div",
                    is_repeated_card=False,
                    rect={
                        "x": 100,
                        "y": 0,
                        "width": 800 * subject_overlap,
                        "height": 600,
                    },
                    web_obstruction_layer="#ordinary-overlay",
                    web_obstruction_layer_structural=True,
                    web_obstruction_layer_kind="overlay",
                    is_advertisement=advertisement_overlay,
                    fixed_or_sticky_ancestor=False,
                    style={"position": "absolute"},
                    hit_test={"visual_occlusion_detected": False},
                ),
                _element(
                    "#ordinary-overlay > button",
                    tag="button",
                    role="button",
                    text="关闭",
                    is_repeated_card=False,
                    interactive=True,
                    rect={"x": 310, "y": 10, "width": 20, "height": 20},
                    effective_z_index=100,
                    web_obstruction_layer="#ordinary-overlay",
                    web_obstruction_layer_structural=True,
                    web_obstruction_layer_kind="overlay",
                    is_advertisement=advertisement_overlay,
                    web_obstruction_close_semantics=True,
                    web_obstruction_close_basis="own_close_token",
                    web_obstruction_close_control_unique=True,
                    web_obstruction_close_hit_safe=True,
                    fixed_or_sticky_ancestor=False,
                    style={"position": "absolute"},
                    hit_test={
                        "center_hits_target": True,
                        "visual_occlusion_detected": False,
                    },
                ),
            ],
        }

    snapshots = {
        "capture-ready": snapshot(after=False),
        "capture-after": snapshot(after=True),
    }

    class Evidence(_ReadyEvidence):
        async def read_snapshot(self, *, capture_id):
            return snapshots[capture_id]

        async def read_url(self, *, capture_id):
            return source_url

        async def read_ready(self, *, capture_id, page_role):
            ready = await super().read_ready(capture_id=capture_id, page_role=page_role)
            return SimpleNamespace(
                **vars(ready),
                screenshot_artifact_id="artifact-after-screenshot",
                dom_artifact_id="artifact-after-dom",
            )

    class Coordinator:
        async def coordinate(self, _command):
            return SimpleNamespace(
                interaction=SimpleNamespace(execution_outcome="succeeded", status="succeeded"),
                before_capture=SimpleNamespace(
                    capture_id="capture-before",
                    parent_capture_id="capture-ready",
                    action_execution_id="scroll-1",
                ),
                after_capture=SimpleNamespace(
                    capture_id="capture-after",
                    parent_capture_id="capture-before",
                    action_execution_id="scroll-1",
                ),
            )

    class Assessor:
        def __init__(self) -> None:
            self.calls = []

        async def assess_obstruction(self, **kwargs):
            self.calls.append(kwargs)
            candidate_id = kwargs["candidates"][0]["candidate_id"] if obstruction_present else None
            return WebObstructionAssessment(
                obstruction_present=obstruction_present,
                dismiss_candidate_id=candidate_id,
                confidence=0.99,
                reason="随机域结构的模型遮挡复核",
                human_verification_present=False,
                age_gate_present=False,
            )

    assessor = Assessor()
    result = await WebPrimaryContentCaptureService(
        coordinator=Coordinator(),
        evidence_source=Evidence(),
        obstruction_assessor=assessor,
    ).capture(
        task_id="task-random",
        agent_run_id="run-random",
        session_id="web:run-random",
        capture_id="capture-ready",
        page_url=source_url,
        page_type="content_detail",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )

    assert result.status == expected_status
    assert len(assessor.calls) == expected_calls
    if expected_calls:
        assert assessor.calls[0]["screenshot_artifact_id"] == "artifact-after-screenshot"
        assert assessor.calls[0]["dom_artifact_id"] == "artifact-after-dom"
        assert len(assessor.calls[0]["candidates"]) == 1
    if obstruction_present and expected_calls:
        assert result.reason_code == "WEB_PRIMARY_CONTENT_OBSTRUCTION_APPEARED"


@pytest.mark.asyncio
async def test_primary_content_capture_stops_after_two_ad_blocked_scrolls() -> None:
    snapshots = {
        capture_id: {
            "viewport": {"width": 1000, "height": 800},
            "elements": [
                _element("#body", is_repeated_card=False),
                _element(
                    "#ad",
                    is_advertisement=True,
                    rect={"x": 0, "y": 0, "width": 1000, "height": 640},
                ),
            ],
        }
        for capture_id in ("capture-ready", "capture-after-1", "capture-after-2")
    }

    class Evidence(_ReadyEvidence):
        async def read_snapshot(self, *, capture_id):
            return snapshots[capture_id]

        async def read_url(self, *, capture_id):
            return "https://example.com/article/42"

    class Coordinator:
        def __init__(self):
            self.calls = 0

        async def coordinate(self, _command):
            self.calls += 1
            return SimpleNamespace(
                before_capture=SimpleNamespace(
                    capture_id=f"before-{self.calls}",
                    parent_capture_id=_command.intent.source_capture_id,
                    action_execution_id=f"scroll-{self.calls}",
                ),
                after_capture=SimpleNamespace(
                    capture_id=f"capture-after-{self.calls}",
                    parent_capture_id=f"before-{self.calls}",
                    action_execution_id=f"scroll-{self.calls}",
                ),
                interaction=SimpleNamespace(execution_outcome="succeeded", status="succeeded"),
            )

    coordinator = Coordinator()
    result = await WebPrimaryContentCaptureService(
        coordinator=coordinator,
        evidence_source=Evidence(),
    ).capture(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="capture-ready",
        page_url="https://example.com/article/42",
        page_type="content_detail",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )

    assert coordinator.calls == 2
    assert result.scroll_count == 2
    assert result.status == "blocked"
    assert result.reason_code == "WEB_PRIMARY_CONTENT_VIEWPORT_BLOCKED"


@pytest.mark.asyncio
async def test_missing_geometry_has_explicit_outcome_without_unbounded_scroll():
    class Evidence(_ReadyEvidence):
        async def read_snapshot(self, **kwargs):
            return {"elements": [_element("#body", is_repeated_card=False)]}

        async def read_url(self, **kwargs):
            return "https://example.test/article/1"

    class Coordinator:
        async def coordinate(self, command):
            return SimpleNamespace(
                before_capture=SimpleNamespace(
                    capture_id="before", parent_capture_id="baseline", action_execution_id="action"
                ),
                after_capture=SimpleNamespace(
                    capture_id="after", parent_capture_id="before", action_execution_id="action"
                ),
                interaction=SimpleNamespace(execution_outcome="succeeded", status="succeeded"),
            )

    result = await WebPrimaryContentCaptureService(
        coordinator=Coordinator(), evidence_source=Evidence()
    ).capture(
        task_id="t",
        agent_run_id="r",
        session_id="web:r",
        capture_id="baseline",
        page_url="https://example.test/article/1",
        page_type="content_detail",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert result.status == "blocked"
    assert result.reason_code == "WEB_PRIMARY_CONTENT_GEOMETRY_UNKNOWN"
    assert result.before_ad_coverage is None
    assert result.after_ad_coverage is None
    assert result.scroll_count <= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("triggered", [False, True])
async def test_after_capture_failure_keeps_actual_action_and_before_evidence(triggered):
    from services.api.app.application.capture_coordination import (
        CaptureCoordinationError,
        CaptureCoordinationStage,
    )

    class Evidence:
        async def read_snapshot(self, **kwargs):
            return {"viewport": {"width": 1000, "height": 800}, "elements": [_element("#body")]}

    class Coordinator:
        async def coordinate(self, command):
            raise CaptureCoordinationError(
                stage=CaptureCoordinationStage.AFTER_CAPTURE
                if triggered
                else CaptureCoordinationStage.BEFORE_CAPTURE,
                error_code="AFTER_CAPTURE_FAILED",
                message="内部错误不能泄漏",
                before_capture=SimpleNamespace(
                    capture_id="actual-before", action_execution_id="actual-action"
                ),
                interaction=SimpleNamespace(execution_outcome="succeeded", status="succeeded")
                if triggered
                else None,
            )

    result = await WebPrimaryContentCaptureService(
        coordinator=Coordinator(), evidence_source=Evidence()
    ).capture(
        task_id="t",
        agent_run_id="r",
        session_id="web:r",
        capture_id="baseline",
        page_url="https://example.test/article/1",
        page_type="content_detail",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert result.status == "failed"
    assert result.reason_code == "AFTER_CAPTURE_FAILED"
    assert result.attempts[0]["before_capture_id"] == "actual-before"
    assert result.attempts[0]["action_execution_id"] == "actual-action"
    assert result.attempts[0]["after_capture_id"] is None
    assert result.interaction_outcomes[0].command_accepted is triggered
    assert result.scroll_count == int(triggered)
