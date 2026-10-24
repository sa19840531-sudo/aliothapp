"""Web 列表到一层可信详情的候选与导航合同。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.capture_coordination import (
    CaptureCoordinationError,
    CaptureCoordinationStage,
)
from services.api.app.application.web_assessment_errors import (
    WebAssessmentPermissionDeniedError,
)
from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_exploration_budget_ledger import (
    WebDetailActionReservation,
)
from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
from services.api.app.application.web_interaction_facts import web_capture_content_facts
from services.api.app.application.web_list_detail_navigation import (
    WebDetailSelectionAssessment,
    WebListDetailEvidence,
    WebListDetailNavigator,
    detail_candidates_from_ready_snapshot,
)
from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebPageStructureFacts,
    WebReadyPageCapture,
)
from services.api.app.application.web_page_readiness import WebPageReadinessResult
from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry
from services.api.app.application.web_subject_region_model import WebSubjectRegionFacts
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.exploration.web_exploration import (
    build_web_obstruction_candidates,
)
from services.api.app.domain.page import WebMenuDecision, WebSiteMenuEntry
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.tests.web_browser_test_support import (
    WebBrowserUnavailable,
    launch_web_test_browser,
)

ORIGIN = "https://example.test"


def _trusted_detail_facts(url, *, subject_url=None):
    return replace(
        web_capture_content_facts(
            {
                "elements": [
                    {
                        "tag": "video",
                        "visible": True,
                        "src": subject_url or "/media/movie.mp4",
                        "selector": "#player",
                    },
                ]
            },
            url=url,
        ),
        readiness_accepted=True,
        identity_verified=True,
    )


def _detail_command() -> RunWebWorkflowCommand:
    return RunWebWorkflowCommand(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        session_owner="web-v2-workflow",
        target_url=ORIGIN,
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )


def _detail_entry() -> WebSiteMenuEntry:
    return WebSiteMenuEntry(
        entry_id="menu-entry-1",
        menu_path=("列表",),
        href="/list",
        canonical_url=f"{ORIGIN}/list",
        decision=WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY,
        is_same_origin=True,
    )


def _runtime_detail_entry(*, menu_depth: int) -> WebRuntimeMenuEntry:
    return WebRuntimeMenuEntry.from_candidate(
        WebNavigationCandidate(
            entry_id="runtime-menu-entry-1",
            label="深层列表",
            url=f"{ORIGIN}/list",
            menu_path=("一级", "二级", "三级", "深层列表"),
            dom_fingerprint="runtime-menu-fingerprint",
            source_signal="trusted_navigation",
            page_type_key="video_list",
            source_capture_id="capture-list",
            source_page_url=ORIGIN,
            menu_depth=menu_depth,
            exploration_path=("一级", "二级", "三级", "深层列表"),
        )
    )


class DetailBudgetLedger:
    async def reserve_detail(self, *, command, entry, **kwargs):
        del kwargs
        return WebDetailActionReservation(
            site_id="example",
            menu_entry_id=entry.entry_id,
            intent_id=f"web-detail:{command.agent_run_id}:example:{entry.entry_id}",
            already_reserved=False,
        )


def _element(**overrides):
    element = {
        "selector": "#card-1 a",
        "tag": "a",
        "role": "link",
        "text": "视频标题一",
        "href": "/video/1",
        "rect": {"x": 100, "y": 200, "width": 300, "height": 180},
        "interactive": True,
        "visible": True,
        "hit_test": {"center_hits_target": True},
        "within_repeated_structure": True,
        "ancestor_class_tokens": ["video-card", "video-grid"],
    }
    element.update(overrides)
    return element


def _snapshot(*elements, capture_id: str = "capture-list", **overrides):
    snapshot = {
        "capture_id": capture_id,
        "structure_signature": "signature-list",
        "elements": list(elements),
    }
    snapshot.update(overrides)
    return snapshot


@pytest.mark.parametrize("x,y", ((638, -148.89), (638, -1), (-1, 40), (1900, 40), (100, 1070)))
def test_detail_binding_does_not_clamp_offscreen_coordinates_into_visibility(x, y) -> None:
    from services.api.app.application.web_list_detail_navigation import (
        build_web_detail_target_observation,
    )

    candidate = detail_candidates_from_ready_snapshot(
        _snapshot(_element()),
        list_page_type="video_list",
        origin=ORIGIN,
    )[0]
    observation = build_web_detail_target_observation(
        _snapshot(
            _element(rect={"x": x, "y": y, "width": 644, "height": 180}),
            viewport={"width": 1920, "height": 1080, "scroll_y": 1836},
        ),
        capture_id="capture-after-scroll",
        candidate=candidate,
    )
    assert observation.target_visible is False
    assert observation.target_hit_test_passed is False


def test_video_list_selects_visible_repeated_card_detail() -> None:
    candidates = detail_candidates_from_ready_snapshot(
        _snapshot(_element()),
        list_page_type="video_list",
        origin=ORIGIN,
    )

    assert candidates[0].expected_page_type == "video_detail"
    assert candidates[0].canonical_url == "https://example.test/video/1"
    assert candidates[0].candidate_id.startswith("web-candidate:")
    assert "selector" not in candidates[0].to_public_dict()
    assert "canonical_url" not in candidates[0].to_public_dict()


@pytest.mark.parametrize("prefix", ("#/", "#!/"))
def test_spa_detail_survives_shared_document_and_root_path(prefix: str) -> None:
    candidates = detail_candidates_from_ready_snapshot(
        _snapshot(_element(href=f"/{prefix}detail/1?id=42")),
        list_page_type="video_list",
        origin=ORIGIN,
        current_url=f"{ORIGIN}/{prefix}list",
    )
    assert tuple(item.canonical_url for item in candidates) == (f"{ORIGIN}/{prefix}detail/1?id=42",)


def test_http_origin_accepts_same_host_https_upgrade_detail() -> None:
    candidates = detail_candidates_from_ready_snapshot(
        _snapshot(_element(href="https://example.test/video/1")),
        list_page_type="video_list",
        origin="http://example.test",
    )

    assert tuple(item.canonical_url for item in candidates) == ("https://example.test/video/1",)


@pytest.mark.parametrize(
    ("origin", "current_url", "href"),
    (
        (ORIGIN, f"{ORIGIN}/list", "/list"),
        (ORIGIN, f"{ORIGIN}/list", f"{ORIGIN}/list"),
        (ORIGIN, f"{ORIGIN}/list", "#comments"),
        (ORIGIN, f"{ORIGIN}/list", "/"),
        (ORIGIN, f"{ORIGIN}/list", "/index.html"),
        (ORIGIN, f"{ORIGIN}/list", "/home"),
    ),
)
def test_current_menu_and_home_aliases_are_not_detail_candidates(
    origin: str,
    current_url: str,
    href: str,
) -> None:
    assert (
        detail_candidates_from_ready_snapshot(
            _snapshot(_element(href=href)),
            list_page_type="video_list",
            origin=origin,
            current_url=current_url,
        )
        == ()
    )


def test_https_origin_rejects_same_host_http_downgrade_detail() -> None:
    assert (
        detail_candidates_from_ready_snapshot(
            _snapshot(_element(href="http://example.test/video/1")),
            list_page_type="video_list",
            origin="https://example.test",
        )
        == ()
    )


@pytest.mark.parametrize(
    "element",
    [
        _element(href=""),
        _element(within_navigation=True),
        _element(within_pagination=True),
        _element(href="https://ads.example/ad", is_advertisement=True),
        _element(visible=False),
        _element(within_repeated_structure=False),
    ],
)
def test_untrusted_structures_do_not_create_detail_candidate(element) -> None:
    assert (
        detail_candidates_from_ready_snapshot(
            _snapshot(element),
            list_page_type="content_list",
            origin=ORIGIN,
        )
        == ()
    )


@pytest.mark.parametrize("kind", ["category", "pagination", "advertisement", "external"])
def test_non_detail_repeated_links_are_excluded(kind: str) -> None:
    """卡片重复结构不能让分类、翻页、推广或站外链接成为详情入口。"""

    overrides = {
        "category": {
            "href": "/category/drama",
            "ancestor_class_tokens": ["category-card", "content-grid"],
        },
        "pagination": {"href": "/list?page=2", "within_pagination": True},
        "advertisement": {"href": "/article/ad", "is_advertisement": True},
        "external": {"href": "https://outside.example/video/1"},
    }

    assert (
        detail_candidates_from_ready_snapshot(
            _snapshot(_element(**overrides[kind])),
            list_page_type="video_list",
            origin=ORIGIN,
        )
        == ()
    )


def test_offscreen_repeated_anchor_is_trusted_from_ready_dom() -> None:
    candidates = detail_candidates_from_ready_snapshot(
        _snapshot(
            _element(
                href="/archives/113850/",
                rect={"x": 100, "y": 1380, "width": 300, "height": 180},
                hit_test={"center_hits_target": False},
                ancestor_class_tokens=["video-item", "video-list"],
            )
        ),
        list_page_type="video_list",
        origin=ORIGIN,
    )

    assert tuple(item.canonical_url for item in candidates) == (
        "https://example.test/archives/113850/",
    )


def test_same_card_primary_anchor_is_deduplicated_and_noise_is_excluded() -> None:
    shared = {
        "href": "/detail/3690/",
        "ancestor_class_tokens": ["hg-drama-card", "drama-grid"],
    }
    candidates = detail_candidates_from_ready_snapshot(
        _snapshot(
            _element(selector="#card-1 a.cover", text="", **shared),
            _element(selector="#card-1 a.title", text="剧情标题", **shared),
            _element(
                selector="#card-1 a.tag",
                text="热门标签",
                href="/tag/hot",
                ancestor_class_tokens=["hg-tag", "meta"],
            ),
            _element(
                selector="#tip_modal a.address",
                text="备用网址一",
                href="/address/1",
                ancestor_class_tokens=["modal", "address-list"],
                web_obstruction_layer="#tip_modal",
                web_obstruction_layer_structural=True,
            ),
            _element(
                selector="footer a.legal",
                text="隐私政策",
                href="/privacy",
                ancestor_class_tokens=["footer", "legal"],
            ),
        ),
        list_page_type="video_list",
        origin=ORIGIN,
    )

    assert len(candidates) == 1
    assert candidates[0].canonical_url == "https://example.test/detail/3690/"
    assert candidates[0].text == "剧情标题"


def test_detail_card_survives_taxonomy_named_ancestor_container() -> None:
    """分类列表容器名不能把其内部真实详情卡片误判为筛选链接。"""

    candidates = detail_candidates_from_ready_snapshot(
        _snapshot(
            _element(
                selector=(
                    "#list_categories_categories_list > section > div > div:nth-of-type(1) > a"
                ),
                text="",
                href="/theme/detail/17/update",
                repeat_group_size=12,
                ancestor_class_tokens=[
                    "img-box",
                    "video-img-box",
                    "col-lg-3",
                ],
                web_detail_priority_candidate=True,
            )
        ),
        list_page_type="video_list",
        origin=ORIGIN,
        current_url=f"{ORIGIN}/theme/",
    )

    assert tuple(item.canonical_url for item in candidates) == (
        "https://example.test/theme/detail/17/update",
    )


@pytest.mark.asyncio
async def test_production_snapshot_marks_repeated_cards_and_excludes_navigation() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <base href="https://example.test/">
    <style>
      nav, .content-grid, .pagination, .advert-banner { display:flex; gap:12px; }
      a { display:block; width:240px; height:80px; }
    </style>
    <nav class="main-navigation">
      <a href="/home">首页</a><a href="/category">分类</a>
    </nav>
    <main class="content-grid">
      <article class="content-card"><a href="/article/1">内容标题一</a></article>
      <article class="content-card"><a href="/article/2">内容标题二</a></article>
    </main>
    <div class="pagination"><a href="/page/1">1</a><a href="/page/2">2</a></div>
    <div class="advert-banner"><a href="/article/ad">推广内容</a></div>
    <footer><a href="/policy/1">政策一</a><a href="/policy/2">政策二</a></footer>
    """

    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(
                page,
                web_detail_candidate_priority=True,
            )
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    by_text = {item.get("text"): item for item in snapshot["elements"]}
    tags = {item.get("tag") for item in snapshot["elements"]}
    assert {"main", "article"}.issubset(tags)
    assert by_text["内容标题一"]["within_repeated_structure"] is True
    assert by_text["内容标题一"]["repeat_group_size"] == 2
    assert by_text["首页"]["within_navigation"] is True
    assert by_text["1"]["within_pagination"] is True
    assert by_text["推广内容"]["is_advertisement"] is True
    assert by_text["政策一"]["within_footer"] is True

    candidates = detail_candidates_from_ready_snapshot(
        {**snapshot, "capture_id": "capture-list"},
        list_page_type="content_list",
        origin="https://example.test",
        current_url="https://example.test/list",
    )
    assert tuple(item.text for item in candidates) == ("内容标题一", "内容标题二")


@pytest.mark.asyncio
async def test_web_navigation_scope_does_not_inherit_body_nav_class() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <base href="https://example.test/">
    <style>
      ul, .content-grid { display:flex; gap:12px; }
      a { display:block; width:240px; height:80px; }
    </style>
    <body class="use-navbar">
      <div class="top-nav-shell">
        <ul><li><a href="/home">首页</a></li><li><a href="/category">分类</a></li></ul>
      </div>
      <main class="content-grid">
        <article class="content-card"><a href="/article/1">正文一</a></article>
        <article class="content-card"><a href="/article/2">正文二</a></article>
      </main>
    </body>
    """

    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(
                page,
                web_detail_candidate_priority=True,
            )
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    by_text = {item.get("text"): item for item in snapshot["elements"]}
    assert by_text["首页"]["within_web_navigation"] is True
    assert by_text["正文一"]["within_web_navigation"] is False

    candidates = detail_candidates_from_ready_snapshot(
        {**snapshot, "capture_id": "capture-body-navbar"},
        list_page_type="content_list",
        origin="https://example.test",
        current_url="https://example.test/list",
    )
    assert tuple(item.text for item in candidates) == ("正文一", "正文二")


@pytest.mark.asyncio
async def test_web_snapshot_prioritizes_detail_anchors_after_dense_controls() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    controls = "".join(
        f'<button style="width:40px;height:20px">控件{index}</button>' for index in range(320)
    )
    html = f"""
    <base href="http://example.test/">
    {controls}
    <main class="content-grid" style="display:flex;gap:12px">
      <article class="content-card"><a style="display:block;width:240px;height:80px" href="https://example.test/article/1">详情一</a></article>
      <article class="content-card"><a style="display:block;width:240px;height:80px" href="https://example.test/article/2">详情二</a></article>
    </main>
    """

    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(
                page,
                web_detail_candidate_priority=True,
            )
            await browser.close()
    except TypeError:
        raise
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    candidates = detail_candidates_from_ready_snapshot(
        {**snapshot, "capture_id": "capture-dense-list"},
        list_page_type="content_list",
        origin="http://example.test",
        current_url="http://example.test/list",
    )
    assert tuple(item.text for item in candidates) == ("详情一", "详情二")
    assert all(
        item.get("web_detail_priority_candidate") is True
        for item in snapshot["elements"]
        if item.get("tag") == "a" and item.get("text") in {"详情一", "详情二"}
    )


def test_detail_selection_reason_is_single_line_and_redacts_public_contacts() -> None:
    assessment = WebDetailSelectionAssessment(
        selected_candidate_id="candidate:1",
        confidence=0.95,
        reason="首个候选\n| 联系 public@example.com，电话 010-12345678",
    )

    assert "\n" not in assessment.reason
    assert "public@example.com" not in assessment.reason
    assert "010-12345678" not in assessment.reason
    assert "[redacted-email]" in assessment.reason
    assert "[redacted-phone]" in assessment.reason


@pytest.mark.parametrize(
    "reason",
    (
        '候选包含 {"password":"credential-value"}',
        "Cookie=session_id=credential-value",
        "Authorization: Bearer credential-value",
    ),
)
def test_detail_selection_reason_rejects_credentials(reason: str) -> None:
    with pytest.raises(ValueError, match="凭据"):
        WebDetailSelectionAssessment(
            selected_candidate_id="candidate:1",
            confidence=0.95,
            reason=reason,
        )


@pytest.mark.asyncio
async def test_production_snapshot_collects_only_visible_media_sources() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(
                '<video controls style="width:320px;height:180px">'
                '<source src="/movie.mp4" type="video/mp4"></video>'
                '<audio controls style="width:280px;height:54px">'
                '<source src="/sound.mp3" type="audio/mpeg"></audio>'
                '<video controls hidden style="width:640px;height:360px">'
                '<source src="/hidden.mp4" type="video/mp4"></video>'
            )
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    sources = {item["src"]: item for item in snapshot["elements"] if item.get("tag") == "source"}
    assert "web_detail_candidate_priority_enabled" not in snapshot
    assert all(
        "within_web_navigation" not in item
        and "web_detail_priority_candidate" not in item
        and "web_obstruction_layer" not in item
        for item in snapshot["elements"]
    )
    assert set(sources) == {"/movie.mp4", "/sound.mp3"}
    assert sources["/movie.mp4"]["media_parent_tag"] == "video"
    assert sources["/movie.mp4"]["rect"]["width"] == 320
    assert sources["/movie.mp4"]["rect"]["height"] == 180
    assert sources["/sound.mp3"]["media_parent_tag"] == "audio"
    assert sources["/sound.mp3"]["rect"]["width"] == 280
    assert all(item["interactive"] is False for item in sources.values())
    assert all(item["hit_test"]["center_hits_target"] is False for item in sources.values())


@pytest.mark.asyncio
async def test_production_web_snapshot_binds_same_modal_elements_to_layer() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      #account-panel { position:fixed; inset:100px; z-index:900; display:block; }
      #account-panel button, #account-panel input { width:240px; height:60px; }
    </style>
    <section id="account-panel" role="dialog" aria-modal="true">
      <input name="password" type="password" placeholder="请输入密码">
      <button id="adult-consent">我已满18岁并继续</button>
    </section>
    """
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(
                page,
                web_detail_candidate_priority=True,
            )
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    by_selector = {item.get("selector"): item for item in snapshot["elements"]}
    assert by_selector["#adult-consent"]["web_obstruction_layer"] == "#account-panel"
    assert by_selector['input[name="password"]']["web_obstruction_layer"] == "#account-panel"


@pytest.mark.asyncio
async def test_production_web_snapshot_never_uses_document_root_as_obstruction() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      html, body { position:absolute; inset:0; margin:0; width:100%; min-height:100%; }
      .video-items { display:grid; grid-template-columns:repeat(2, 1fr); gap:16px; }
      .video-item a { display:block; width:320px; height:180px; }
    </style>
    <main class="app-content">
      <section class="video-items search-results">
        <article class="video-item">
          <a id="detail-1" href="/comic/index/detail?id=1">标题一</a>
        </article>
        <article class="video-item">
          <a id="detail-2" href="/comic/index/detail?id=2">标题二</a>
        </article>
      </section>
    </main>
    """
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(
                page,
                web_detail_candidate_priority=True,
            )
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    by_selector = {item.get("selector"): item for item in snapshot["elements"]}
    assert by_selector["#detail-1"]["web_obstruction_layer"] is None
    assert by_selector["#detail-2"]["web_obstruction_layer"] is None
    snapshot["capture_id"] = "capture-root-layout"
    candidates = detail_candidates_from_ready_snapshot(
        snapshot,
        list_page_type="video_list",
        origin=ORIGIN,
        current_url=f"{ORIGIN}/comic/index/search?keyword=demo",
    )
    assert len(candidates) == 2


@pytest.mark.asyncio
async def test_production_web_snapshot_marks_low_z_dialog_close_semantics() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      #site-header { position:fixed; inset:0 0 auto; height:64px; z-index:1900; }
      #site-header ul { display:flex; }
      #dialog { position:fixed; inset:120px 320px; z-index:100; }
      #dialog-close { width:42px; height:42px; }
    </style>
    <header id="site-header">
      <ul class="navbar-nav"><li><a id="login-link" href="/login">登录</a></li></ul>
    </header>
    <section id="dialog" class="van-popup van-popup--center"
             role="dialog" aria-modal="true">
      <div class="close-btn">
        <img id="dialog-close" class="close-image on-dialog-close" alt="关闭"
             src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">
      </div>
    </section>
    """
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(
                page,
                web_detail_candidate_priority=True,
            )
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    by_selector = {item.get("selector"): item for item in snapshot["elements"]}
    close = by_selector["#dialog-close"]
    assert close["web_obstruction_layer"] == "#dialog"
    assert close["web_obstruction_layer_kind"] == "dialog"
    assert close["web_obstruction_layer_structural"] is True
    assert close["web_obstruction_close_semantics"] is True
    assert by_selector["#login-link"]["within_web_navigation"] is True


@pytest.mark.asyncio
async def test_production_web_snapshot_marks_delegated_popup_close_wrapper() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      #popup-window { position:fixed; inset:100px 300px; z-index:2000; }
      .on-popup-close { width:30px; height:30px; cursor:pointer; }
      .close-image { width:30px; height:30px; }
    </style>
    <section id="popup-window" class="van-popup" role="dialog">
      <div id="popup-close" class="close-img on-popup-close">
        <img id="popup-close-image" class="close-image"
             src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">
      </div>
    </section>
    """
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(
                page,
                web_detail_candidate_priority=True,
            )
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    by_selector = {item.get("selector"): item for item in snapshot["elements"]}
    assert by_selector["#popup-close"]["web_obstruction_close_semantics"] is True
    assert by_selector["#popup-close-image"]["web_obstruction_close_semantics"] is True
    candidates = build_web_obstruction_candidates(
        capture_id="capture-popup",
        ui_snapshot=snapshot,
    )
    assert len(candidates) == 1
    assert candidates[0].selector == "#popup-close"
    assert candidates[0].capability == "web_obstruction_dismiss"


@pytest.mark.asyncio
async def test_production_web_snapshot_marks_low_z_geometric_popup_without_role_or_class() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      #popup-window { position:fixed; inset:140px 460px; z-index:100; background:#fff; }
      #popup-close { width:48px; height:48px; cursor:pointer; }
    </style>
    <main><p>页面正文</p></main>
    <section id="popup-window">
      <button id="popup-close" aria-label="关闭">×</button>
    </section>
    """
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(
                page,
                web_detail_candidate_priority=True,
            )
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    close = next(item for item in snapshot["elements"] if item.get("selector") == "#popup-close")
    assert close["web_obstruction_layer"] == "#popup-window"
    assert close["web_obstruction_layer_structural"] is True
    assert close["web_obstruction_close_semantics"] is True
    candidates = build_web_obstruction_candidates(
        capture_id="capture-popup-low-z",
        ui_snapshot=snapshot,
    )
    assert len(candidates) == 1
    assert candidates[0].selector == "#popup-close"


@pytest.mark.asyncio
async def test_production_web_snapshot_separates_independent_fixed_panels() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      html, body { overflow:hidden; }
      .panel { position:fixed; z-index:100; width:600px; height:350px; }
      .age-panel { left:100px; top:100px; }
      .login-panel { right:100px; top:100px; }
      .panel button { width:240px; height:60px; }
    </style>
        <section class="panel age-panel age-gate" role="dialog">
      <button id="adult-entry">我已年满18岁并进入</button>
    </section>
        <section class="panel login-panel adult-gate register-modal" role="dialog">
      <button id="registration-adult">我确认已满18岁并同意用户条款</button>
      <input id="registration-password" type="password">
    </section>
    """
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(
                page,
                web_detail_candidate_priority=True,
            )
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    by_selector = {item.get("selector"): item for item in snapshot["elements"]}
    age_layer = by_selector["#adult-entry"]["web_obstruction_layer"]
    registration_layer = by_selector["#registration-adult"]["web_obstruction_layer"]
    assert age_layer
    assert registration_layer
    assert age_layer != registration_layer
    assert by_selector["#registration-adult"]["web_obstruction_layer_kind"] == ("age_gate")
    assert by_selector["#registration-adult"]["web_obstruction_close_semantics"] is True

    candidates = build_web_obstruction_candidates(
        capture_id="capture-independent-panels",
        ui_snapshot=snapshot,
    )
    age_gate = next(item for item in candidates if item.text == "我已年满18岁并进入")
    assert age_gate.capability == "web_age_gate_confirm"
    assert all(item.selector != "#registration-adult" for item in candidates)
    assert all("同意用户条款" not in item.text for item in candidates)


@pytest.mark.asyncio
async def test_production_web_snapshot_recognizes_age_verify_gate() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      #age-verify-overlay { position:fixed; inset:0; z-index:100000; }
      .age-verify-modal { width:490px; margin:300px auto; }
      .age-verify-btn { width:432px; height:50px; }
    </style>
    <section id="age-verify-overlay" class="age-verify-overlay">
      <div class="age-verify-modal">
        <h2>请确定你已年满18岁</h2>
        <button id="age-verify-leave" class="age-verify-btn age-verify-btn-leave">
          我未满18岁 - 离开
        </button>
        <button id="age-verify-enter" class="age-verify-btn age-verify-btn-enter">
          我年满18岁 - 进入
        </button>
      </div>
    </section>
    """
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(
                page,
                web_detail_candidate_priority=True,
            )
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    by_selector = {item.get("selector"): item for item in snapshot["elements"]}
    adult_entry = by_selector["#age-verify-enter"]
    assert adult_entry["web_obstruction_layer_kind"] == "age_gate"
    assert adult_entry["web_obstruction_close_semantics"] is True
    candidates = build_web_obstruction_candidates(
        capture_id="capture-age-verify",
        ui_snapshot=snapshot,
    )
    assert [(item.selector, item.capability) for item in candidates] == [
        ("#age-verify-enter", "web_age_gate_confirm")
    ]


@pytest.mark.asyncio
async def test_production_web_snapshot_recognizes_age_modal_gate() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .hg-age-modal { position:fixed; inset:0; z-index:2147483001; }
      .hg-age-modal__panel { width:500px; margin:300px auto; }
      .hg-age-modal__btn { width:500px; height:58px; }
    </style>
    <section class="hg-age-modal" role="dialog">
      <div class="hg-age-modal__panel">
        <p>本站含有成人内容，仅限年满 18 周岁的成年人访问。</p>
        <button id="hg-age-enter" class="hg-age-modal__btn hg-age-modal__btn--enter">
          我已年满 18 周岁，进入网站
        </button>
        <button id="hg-age-leave" class="hg-age-modal__btn hg-age-modal__btn--leave">
          未满 18 周岁，离开
        </button>
      </div>
    </section>
    """
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(
                page,
                web_detail_candidate_priority=True,
            )
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    by_selector = {item.get("selector"): item for item in snapshot["elements"]}
    adult_entry = by_selector["#hg-age-enter"]
    assert adult_entry["web_obstruction_layer_kind"] == "age_gate"
    assert adult_entry["web_obstruction_close_semantics"] is True
    candidates = build_web_obstruction_candidates(
        capture_id="capture-age-modal",
        ui_snapshot=snapshot,
    )
    assert [(item.selector, item.capability) for item in candidates] == [
        ("#hg-age-enter", "web_age_gate_confirm")
    ]


@pytest.mark.parametrize(
    (
        "list_page_type",
        "classified_page_type",
        "final_url",
        "expected_status",
        "expected_error_code",
    ),
    (
        ("video_list", "video_detail", f"{ORIGIN}/video/1", "detail_captured", None),
        ("video_list", "video_detail", f"{ORIGIN}/list", "detail_captured", None),
        ("content_list", "video_detail", f"{ORIGIN}/video/1", "detail_captured", None),
        ("video_list", "content_detail", f"{ORIGIN}/video/1", "detail_captured", None),
        ("content_list", "content_detail", f"{ORIGIN}/video/1", "detail_captured", None),
        (
            "content_list",
            "profile",
            f"{ORIGIN}/video/1",
            "detail_page_type_mismatch",
            "WEB_DETAIL_PAGE_TYPE_MISMATCH",
        ),
        (
            "video_list",
            "video_detail",
            f"{ORIGIN}/list",
            "navigation_failed",
            "WEB_DETAIL_POSTCONDITION_NOT_SATISFIED",
        ),
    ),
)
@pytest.mark.asyncio
async def test_selected_detail_validates_closed_detail_page_type(
    list_page_type: str,
    classified_page_type: str,
    final_url: str,
    expected_status: str,
    expected_error_code: str | None,
) -> None:
    snapshot = _snapshot(
        _element(),
        _element(
            selector="#card-2 a",
            text="视频标题二",
            href="/video/2",
            rect={"x": 420, "y": 200, "width": 300, "height": 180},
        ),
    )
    candidate = detail_candidates_from_ready_snapshot(
        snapshot,
        list_page_type=list_page_type,
        origin=ORIGIN,
    )[0]
    intents = []

    class Selector:
        async def select_detail_candidate(self, **kwargs):
            assert all("selector" not in item for item in kwargs["candidates"])
            assert all("canonical_url" not in item for item in kwargs["candidates"])
            return WebDetailSelectionAssessment(
                selected_candidate_id=candidate.candidate_id,
                confidence=0.75,
                reason="首个可见重复视频卡片",
            )

    class Coordinator:
        async def coordinate(self, command):
            intents.append(command.intent)
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id="capture-detail-probe"),
                interaction=SimpleNamespace(
                    execution_outcome="succeeded",
                    status="succeeded",
                    action_execution_id="actual-detail-action",
                ),
            )

    class Readiness:
        async def prepare(self, *, source_capture_id, page_role):
            assert source_capture_id == "capture-detail-probe"
            assert page_role == "detail"
            return WebPageReadinessResult(
                status="ready",
                ready_capture_id="capture-detail-ready",
                dismissed_layers=(),
                evidence_capture_ids=(
                    "capture-detail-probe",
                    "capture-detail-ready",
                ),
            )

    class ReadyCaptureReader:
        async def read(self, *, capture_id, page_role):
            assert capture_id == "capture-detail-ready"
            assert page_role == "detail"
            return WebReadyPageCapture(
                capture_id=capture_id,
                interaction_facts=_trusted_detail_facts(
                    final_url,
                    subject_url=("/video/1" if expected_status == "detail_captured" else None),
                ),
                web_capture_stage="web_page_ready_baseline",
                screenshot_artifact_id="detail:screenshot",
                dom_artifact_id="detail:dom",
                visible_text_artifact_id="detail:text",
                page_role="detail",
                structure_facts=WebPageStructureFacts(
                    navigation_element_count=0,
                    repeated_link_count=0,
                    video_element_count=1,
                    single_subject_element_count=1,
                    dom_tokens=("video", "player"),
                    subject_region_facts=WebSubjectRegionFacts(
                        primary_subject_count=1,
                        primary_subject_kind="video",
                        primary_player_count=1,
                    ),
                ),
            )

    class CaptureUrlReader:
        async def read_url(self, *, capture_id):
            assert capture_id == "capture-detail-probe"
            return final_url

    class Classifier:
        async def classify(self, capture):
            assert capture.classification_hint is not None
            assert capture.classification_hint.expected_page_type == (
                "video_detail" if list_page_type == "video_list" else "content_detail"
            )
            assert capture.classification_hint.source == "trusted_list_detail_transition"
            assert capture.structure_facts.subject_region_facts.detail_navigation_source_count == 1
            assert dict(capture.structure_facts.subject_region_facts.selector_summaries)[
                "detail_navigation_source"
            ]
            return WebPageAssessment(
                page_type=classified_page_type,
                confidence=0.97,
                evidence=("DOM 存在播放器", "页面为单一视频主体"),
            )

    result = await WebListDetailNavigator(
        selector=Selector(),
        coordinator=Coordinator(),
        readiness=Readiness(),
        ready_capture_reader=ReadyCaptureReader(),
        classifier=Classifier(),
        capture_url_reader=CaptureUrlReader(),
        budget_ledger=DetailBudgetLedger(),
    ).capture_one(
        WebListDetailEvidence(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            ready_capture_id="capture-list",
            screenshot_artifact_id="list:screenshot",
            dom_artifact_id="list:dom",
            origin=ORIGIN,
            current_url=f"{ORIGIN}/list",
            ui_snapshot=snapshot,
            deadline=datetime.now(UTC) + timedelta(minutes=2),
        ),
        list_page_type=list_page_type,
        command=_detail_command(),
        entry=_detail_entry(),
    )

    assert result.status == expected_status
    assert result.error_code == expected_error_code
    if expected_error_code == "WEB_DETAIL_POSTCONDITION_NOT_SATISFIED":
        assert result.detail_capture_id == "capture-detail-ready"
        assert result.page_type is None
        assert result.readiness_evidence_capture_ids == (
            "capture-detail-probe",
            "capture-detail-ready",
        )
        assert result.interaction_outcome is not None
        assert result.interaction_outcome.command_accepted is True
        assert result.interaction_outcome.effect_observed is True
        assert result.interaction_outcome.transition_arrived is False
    else:
        assert result.detail_capture_id == "capture-detail-ready"
        assert result.page_type == classified_page_type
        assert result.interaction_outcome is not None
        assert result.interaction_outcome.transition_arrived is (classified_page_type != "profile")
    assert result.selected_candidate_id == candidate.candidate_id
    assert result.detail_candidate_ids == tuple(
        item.candidate_id
        for item in detail_candidates_from_ready_snapshot(
            snapshot,
            list_page_type=list_page_type,
            origin=ORIGIN,
        )
    )
    assert result.detail_selection_confidence == 0.75
    assert result.detail_selection_reason == "首个可见重复视频卡片"
    assert result.detail_action_count == 1
    assert result.probe_capture_id == "capture-detail-probe"
    if expected_error_code != "WEB_DETAIL_POSTCONDITION_NOT_SATISFIED":
        assert result.readiness_evidence_capture_ids == (
            "capture-detail-probe",
            "capture-detail-ready",
        )
    assert len(intents) == 1
    assert intents[0].action_type.value == "navigate"
    assert intents[0].parameters["target_mode"] == "web_detail_candidate_url"
    assert intents[0].parameters["navigation_depth"] == 2
    assert intents[0].parameters["destination_url"] == candidate.canonical_url
    assert intents[0].parameters["web_detail_candidate_id"] == candidate.candidate_id
    assert len(intents[0].intent_id) <= 128
    assert "selector" not in intents[0].parameters


async def _visit_repeated_cards(*, menu_depth: int, landing: str):
    """以运行时深层菜单进入重复视频卡片，并返回详情落地结果与动作。"""

    snapshot = _snapshot(_element())
    candidate = detail_candidates_from_ready_snapshot(
        snapshot,
        list_page_type="video_list",
        origin=ORIGIN,
    )[0]
    intents = []

    class Selector:
        async def select_detail_candidate(self, **kwargs):
            del kwargs
            return WebDetailSelectionAssessment(
                selected_candidate_id=candidate.candidate_id,
                confidence=0.95,
                reason="重复视频卡片标题与封面均可见",
            )

    class Coordinator:
        async def coordinate(self, command):
            intents.append(command.intent)
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id="capture-detail-probe"),
                interaction=SimpleNamespace(
                    execution_outcome="succeeded",
                    status="succeeded",
                    action_execution_id="deep-detail-action",
                ),
            )

    class Readiness:
        async def prepare(self, *, source_capture_id, page_role):
            assert source_capture_id == "capture-detail-probe"
            assert page_role == "detail"
            return WebPageReadinessResult(
                status="ready",
                ready_capture_id="capture-detail-ready",
                dismissed_layers=(),
                evidence_capture_ids=("capture-detail-probe", "capture-detail-ready"),
            )

    class ReadyCaptureReader:
        async def read(self, *, capture_id, page_role):
            assert capture_id == "capture-detail-ready"
            assert page_role == "detail"
            return WebReadyPageCapture(
                capture_id=capture_id,
                interaction_facts=_trusted_detail_facts(f"{ORIGIN}/video/1"),
                web_capture_stage="web_page_ready_baseline",
                screenshot_artifact_id="detail:screenshot",
                dom_artifact_id="detail:dom",
                visible_text_artifact_id="detail:text",
                page_role="detail",
                structure_facts=WebPageStructureFacts(
                    navigation_element_count=0,
                    repeated_link_count=0,
                    video_element_count=1,
                    single_subject_element_count=1,
                    dom_tokens=("video", "player"),
                ),
            )

    class CaptureUrlReader:
        async def read_url(self, *, capture_id):
            assert capture_id == "capture-detail-probe"
            return f"{ORIGIN}/video/1"

    class Classifier:
        async def classify(self, capture):
            assert capture.classification_hint is not None
            return WebPageAssessment(
                page_type=landing,
                confidence=0.99,
                evidence=("DOM 存在播放器", "单一视频主体"),
            )

    result = await WebListDetailNavigator(
        selector=Selector(),
        coordinator=Coordinator(),
        readiness=Readiness(),
        ready_capture_reader=ReadyCaptureReader(),
        classifier=Classifier(),
        capture_url_reader=CaptureUrlReader(),
        budget_ledger=DetailBudgetLedger(),
    ).capture_one(
        WebListDetailEvidence(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            ready_capture_id="capture-list",
            screenshot_artifact_id="list:screenshot",
            dom_artifact_id="list:dom",
            origin=ORIGIN,
            current_url=f"{ORIGIN}/list",
            ui_snapshot=snapshot,
            deadline=datetime.now(UTC) + timedelta(minutes=2),
        ),
        list_page_type="video_list",
        command=_detail_command(),
        entry=_runtime_detail_entry(menu_depth=menu_depth),
    )
    return result, intents


@pytest.mark.asyncio
async def test_repeated_cards_open_single_subject_detail_beyond_ten_menu_depth() -> None:
    """运行时第 12 层菜单的详情点击必须继续记录为第 13 层。"""

    result, intents = await _visit_repeated_cards(menu_depth=12, landing="video_detail")

    assert result.status is WebMenuTerminalStatus.DETAIL_CAPTURED
    assert result.page_type == "video_detail"
    assert result.selected_candidate_id is not None
    assert intents[-1].parameters["navigation_depth"] == 13


@pytest.mark.parametrize("blocked", [
    "human_verification_blocked", "popup_blocked", "loading", "timeout",
    "artifact_skew", "identity_drift", "capture_failed", "transitional", "degraded",
])
@pytest.mark.asyncio
async def test_after_blocker_retains_receipt_but_never_arrives(blocked):
    snapshot = _snapshot(_element())
    candidate = detail_candidates_from_ready_snapshot(
        snapshot, list_page_type="video_list", origin=ORIGIN
    )[0]

    class Selector:
        async def select_detail_candidate(self, **kwargs):
            return WebDetailSelectionAssessment(
                selected_candidate_id=candidate.candidate_id, confidence=0.9, reason="可信卡片"
            )

    class Coordinator:
        async def coordinate(self, command):
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id="after"),
                interaction=SimpleNamespace(
                    execution_outcome="succeeded", status="succeeded", action_execution_id="action"
                ),
            )

    class Readiness:
        async def prepare(self, **kwargs):
            return WebPageReadinessResult(
                status=blocked,
                ready_capture_id=None,
                dismissed_layers=(),
                evidence_capture_ids=("after",),
            )

    class UrlReader:
        async def read_url(self, **kwargs):
            return f"{ORIGIN}/video/1"

    result = await WebListDetailNavigator(
        selector=Selector(),
        coordinator=Coordinator(),
        readiness=Readiness(),
        ready_capture_reader=object(),
        classifier=object(),
        capture_url_reader=UrlReader(),
        budget_ledger=DetailBudgetLedger(),
    ).capture_one(
        WebListDetailEvidence(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            ready_capture_id="capture-list",
            screenshot_artifact_id="s",
            dom_artifact_id="d",
            origin=ORIGIN,
            current_url=f"{ORIGIN}/list",
            ui_snapshot=snapshot,
            deadline=datetime.now(UTC) + timedelta(minutes=2),
        ),
        list_page_type="video_list",
        command=_detail_command(),
        entry=_detail_entry(),
    )
    assert result.detail_action_count == 1
    assert result.interaction_outcome.command_accepted is True
    assert result.interaction_outcome.effect_observed is True
    assert result.interaction_outcome.transition_arrived is False
    assert result.status == (
        blocked if blocked in {"human_verification_blocked", "popup_blocked"}
        else "navigation_failed"
    )
    assert result.error_code == (
        "WEB_DETAIL_HUMAN_VERIFICATION_BLOCKED" if blocked == "human_verification_blocked"
        else "WEB_DETAIL_POPUP_BLOCKED" if blocked == "popup_blocked"
        else "WEB_DETAIL_READINESS_FAILED"
    )
    assert result.diagnostic_type == f"readiness_{blocked}"


@pytest.mark.asyncio
async def test_offscreen_detail_uses_trusted_url_without_scroll_or_click() -> None:
    snapshot = _snapshot(
        _element(
            rect={"x": 100, "y": 1380, "width": 300, "height": 180},
            hit_test={"center_hits_target": False},
        ),
        document_epoch="epoch-list",
        viewport={"width": 1920, "height": 1080, "scroll_y": 0},
        document={"scroll_height": 2400},
    )
    candidate = detail_candidates_from_ready_snapshot(
        snapshot,
        list_page_type="video_list",
        origin=ORIGIN,
    )[0]
    intents = []

    class Selector:
        async def select_detail_candidate(self, **kwargs):
            del kwargs
            return WebDetailSelectionAssessment(
                selected_candidate_id=candidate.candidate_id,
                confidence=0.99,
                reason="离屏可信详情",
            )

    class Coordinator:
        async def coordinate(self, command):
            intents.append(command.intent)
            assert command.intent.action_type.value == "navigate"
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id="capture-detail-probe"),
                interaction=SimpleNamespace(
                    execution_outcome="succeeded",
                    status="succeeded",
                    action_execution_id="detail-navigate-1",
                ),
            )

    class ObservationReader:
        async def read_detail_target_observation(self, **kwargs):
            raise AssertionError(f"详情 URL 导航不应读取视口绑定: {kwargs}")

    class Readiness:
        async def prepare(self, *, source_capture_id, page_role):
            assert source_capture_id == "capture-detail-probe"
            assert page_role == "detail"
            return WebPageReadinessResult(
                status="ready",
                ready_capture_id="capture-detail-ready",
                dismissed_layers=(),
                evidence_capture_ids=("capture-detail-probe", "capture-detail-ready"),
            )

    class ReadyCaptureReader:
        async def read(self, *, capture_id, page_role):
            assert capture_id == "capture-detail-ready"
            assert page_role == "detail"
            return WebReadyPageCapture(
                capture_id=capture_id,
                interaction_facts=_trusted_detail_facts(candidate.canonical_url),
                web_capture_stage="web_page_ready_baseline",
                screenshot_artifact_id="detail:screenshot",
                dom_artifact_id="detail:dom",
                visible_text_artifact_id="detail:text",
                page_role="detail",
                structure_facts=WebPageStructureFacts(
                    navigation_element_count=0,
                    repeated_link_count=0,
                    video_element_count=1,
                    single_subject_element_count=1,
                    dom_tokens=("video", "player"),
                ),
            )

    class Classifier:
        async def classify(self, capture):
            del capture
            return WebPageAssessment(
                page_type="video_detail",
                confidence=0.99,
                evidence=("详情页",),
            )

    class CaptureUrlReader:
        async def read_url(self, *, capture_id):
            assert capture_id == "capture-detail-probe"
            return candidate.canonical_url

    result = await WebListDetailNavigator(
        selector=Selector(),
        coordinator=Coordinator(),
        readiness=Readiness(),
        ready_capture_reader=ReadyCaptureReader(),
        classifier=Classifier(),
        capture_url_reader=CaptureUrlReader(),
        budget_ledger=DetailBudgetLedger(),
        target_observation_reader=ObservationReader(),
    ).capture_one(
        WebListDetailEvidence(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            ready_capture_id="capture-list",
            screenshot_artifact_id="list:screenshot",
            dom_artifact_id="list:dom",
            origin=ORIGIN,
            current_url=f"{ORIGIN}/list",
            ui_snapshot=snapshot,
            deadline=datetime.now(UTC) + timedelta(minutes=2),
        ),
        list_page_type="video_list",
        command=_detail_command(),
        entry=_detail_entry(),
    )

    assert result.status is WebMenuTerminalStatus.DETAIL_CAPTURED
    assert result.scroll_interaction_outcomes == ()
    assert result.detail_scroll_count == 0
    assert result.target_binding_status == "ready"
    assert [intent.action_type.value for intent in intents] == ["navigate"]
    assert intents[0].source_capture_id == "capture-list"
    assert intents[0].parameters["target_mode"] == "web_detail_candidate_url"
    assert intents[0].parameters["destination_url"] == candidate.canonical_url
    assert intents[0].parameters["navigation_depth"] == 2


@pytest.mark.asyncio
async def test_invalid_or_failed_model_selection_stays_terminal_without_action() -> None:
    snapshot = _snapshot(_element())
    candidate = detail_candidates_from_ready_snapshot(
        snapshot,
        list_page_type="video_list",
        origin=ORIGIN,
    )[0]

    class SelectorFailure:
        async def select_detail_candidate(self, **kwargs):
            del kwargs
            raise ValueError("provider returned an invalid candidate")

    class SelectorLowConfidence:
        async def select_detail_candidate(self, **kwargs):
            del kwargs
            return WebDetailSelectionAssessment(
                selected_candidate_id=candidate.candidate_id,
                confidence=0.69,
                reason="候选代表性不足",
            )

    class NeverCalled:
        async def reserve_detail(self, **kwargs):
            raise AssertionError(kwargs)

        async def coordinate(self, command):
            raise AssertionError(command)

        async def prepare(self, **kwargs):
            raise AssertionError(kwargs)

        async def read(self, **kwargs):
            raise AssertionError(kwargs)

        async def classify(self, *args, **kwargs):
            raise AssertionError((args, kwargs))

        async def read_url(self, **kwargs):
            raise AssertionError(kwargs)

    evidence = WebListDetailEvidence(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        ready_capture_id="capture-list",
        screenshot_artifact_id="list:screenshot",
        dom_artifact_id="list:dom",
        origin=ORIGIN,
        current_url=f"{ORIGIN}/list",
        ui_snapshot=snapshot,
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )

    results = []
    for selector in (SelectorFailure(), SelectorLowConfidence()):
        results.append(
            await WebListDetailNavigator(
                selector=selector,
                coordinator=NeverCalled(),
                readiness=NeverCalled(),
                ready_capture_reader=NeverCalled(),
                classifier=NeverCalled(),
                capture_url_reader=NeverCalled(),
                budget_ledger=NeverCalled(),
            ).capture_one(
                evidence,
                list_page_type="video_list",
                command=_detail_command(),
                entry=_detail_entry(),
            )
        )

    assert [item.status for item in results] == [
        WebMenuTerminalStatus.DETAIL_SELECTION_UNTRUSTED,
        WebMenuTerminalStatus.DETAIL_SELECTION_UNTRUSTED,
    ]
    assert all(item.detail_action_count == 0 for item in results)
    assert all(item.detail_candidate_ids == (candidate.candidate_id,) for item in results)
    assert results[0].error_code == "WEB_DETAIL_SELECTION_FAILED"
    assert results[1].error_code == "WEB_DETAIL_SELECTION_UNTRUSTED"


@pytest.mark.asyncio
async def test_detail_selection_403_is_menu_local_without_action_or_second_candidate() -> None:
    snapshot = _snapshot(_element())
    candidate = detail_candidates_from_ready_snapshot(
        snapshot, list_page_type="video_list", origin=ORIGIN,
    )[0]

    class Selector:
        calls = 0

        async def select_detail_candidate(self, **kwargs):
            del kwargs
            self.calls += 1
            if self.calls == 1:
                raise WebAssessmentPermissionDeniedError(
                    assessment_kind="web_detail_selection"
                )
            raise ValueError("下一菜单候选待复核")

    class NeverCalled:
        def __getattr__(self, name):
            raise AssertionError(name)

    evidence = WebListDetailEvidence(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        ready_capture_id="capture-list",
        screenshot_artifact_id="list:screenshot",
        dom_artifact_id="list:dom",
        origin=ORIGIN,
        current_url=f"{ORIGIN}/list",
        ui_snapshot=snapshot,
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )
    selector = Selector()
    navigator = WebListDetailNavigator(
        selector=selector,
        coordinator=NeverCalled(),
        readiness=NeverCalled(),
        ready_capture_reader=NeverCalled(),
        classifier=NeverCalled(),
        capture_url_reader=NeverCalled(),
        budget_ledger=NeverCalled(),
    )

    denied = await navigator.capture_one(
        evidence, list_page_type="video_list",
        command=_detail_command(), entry=_detail_entry(),
    )
    continued = await navigator.capture_one(
        evidence, list_page_type="video_list",
        command=_detail_command(), entry=_detail_entry(),
    )

    assert selector.calls == 2
    assert denied.status is WebMenuTerminalStatus.DETAIL_SELECTION_UNTRUSTED
    assert denied.error_code == "WEB_DETAIL_SELECTION_MODEL_PERMISSION_DENIED"
    assert denied.detail_selection_reason == denied.error_code
    assert denied.detail_candidate_ids == (candidate.candidate_id,)
    assert denied.detail_action_count == 0
    assert denied.selected_candidate_id is None
    assert denied.detail_selection_diagnostic["reason_code"] == denied.error_code
    assert denied.detail_selection_diagnostic["source_capture_id"] == "capture-list"
    assert continued.status is WebMenuTerminalStatus.DETAIL_SELECTION_UNTRUSTED


@pytest.mark.asyncio
async def test_no_trusted_candidate_is_terminal_without_click() -> None:
    class NeverCalled:
        async def __getattr__(self, name):
            raise AssertionError(name)

    result = await WebListDetailNavigator(
        selector=NeverCalled(),
        coordinator=NeverCalled(),
        readiness=NeverCalled(),
        ready_capture_reader=NeverCalled(),
        classifier=NeverCalled(),
        capture_url_reader=NeverCalled(),
    ).capture_one(
        WebListDetailEvidence(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            ready_capture_id="capture-list",
            screenshot_artifact_id="list:screenshot",
            dom_artifact_id="list:dom",
            origin=ORIGIN,
            current_url=f"{ORIGIN}/list",
            ui_snapshot=_snapshot(),
            deadline=datetime.now(UTC) + timedelta(minutes=2),
        ),
        list_page_type="content_list",
    )

    assert result.status == "no_detail_candidate"
    assert result.detail_capture_id is None
    assert result.detail_action_count == 0


@pytest.mark.asyncio
async def test_detail_cross_origin_redirect_stops_before_readiness() -> None:
    snapshot = _snapshot(_element())
    candidate = detail_candidates_from_ready_snapshot(
        snapshot,
        list_page_type="video_list",
        origin=ORIGIN,
    )[0]
    calls: list[str] = []

    class Selector:
        async def select_detail_candidate(self, **kwargs):
            del kwargs
            return WebDetailSelectionAssessment(
                selected_candidate_id=candidate.candidate_id,
                confidence=0.99,
                reason="当前可信候选",
            )

    class Coordinator:
        async def coordinate(self, command):
            del command
            calls.append("coordinate")
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id="capture-detail-cross"),
                interaction=SimpleNamespace(
                    execution_outcome="succeeded",
                    status="succeeded",
                    action_execution_id="actual-detail-action-cross",
                ),
            )

    class CaptureUrlReader:
        async def read_url(self, *, capture_id):
            assert capture_id == "capture-detail-cross"
            calls.append("read_url")
            return "https://ads.example/landing"

    class NeverReadiness:
        async def prepare(self, **kwargs):
            raise AssertionError(kwargs)

    result = await WebListDetailNavigator(
        selector=Selector(),
        coordinator=Coordinator(),
        readiness=NeverReadiness(),
        ready_capture_reader=NeverReadiness(),
        classifier=NeverReadiness(),
        capture_url_reader=CaptureUrlReader(),
        budget_ledger=DetailBudgetLedger(),
    ).capture_one(
        WebListDetailEvidence(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            ready_capture_id="capture-list",
            screenshot_artifact_id="list:screenshot",
            dom_artifact_id="list:dom",
            origin=ORIGIN,
            current_url=f"{ORIGIN}/list",
            ui_snapshot=snapshot,
            deadline=datetime.now(UTC) + timedelta(minutes=2),
        ),
        list_page_type="video_list",
        command=_detail_command(),
        entry=_detail_entry(),
    )

    assert result.status is WebMenuTerminalStatus.UNSAFE_NAVIGATION
    assert result.error_code == "WEB_NAVIGATION_CROSS_ORIGIN"
    assert result.detail_capture_id is None
    assert result.detail_action_count == 1
    assert calls == ["coordinate", "read_url"]


@pytest.mark.parametrize(
    ("failing_stage", "expected_error_code", "expected_diagnostic"),
    (
        ("read_url", "WEB_DETAIL_URL_READ_FAILED", "详情 URL 读取失败"),
        ("readiness", "WEB_DETAIL_READINESS_FAILED", "详情页就绪准备失败"),
        ("ready_capture", "WEB_DETAIL_EVIDENCE_READ_FAILED", "详情页证据读取失败"),
        ("classifier", "WEB_DETAIL_CLASSIFICATION_FAILED", "详情页分类失败"),
    ),
)
@pytest.mark.asyncio
async def test_post_navigation_failure_keeps_single_action_count(
    failing_stage: str,
    expected_error_code: str,
    expected_diagnostic: str,
) -> None:
    snapshot = _snapshot(_element())
    candidate = detail_candidates_from_ready_snapshot(
        snapshot,
        list_page_type="video_list",
        origin=ORIGIN,
    )[0]
    intents = []

    class Selector:
        async def select_detail_candidate(self, **kwargs):
            del kwargs
            return WebDetailSelectionAssessment(
                selected_candidate_id=candidate.candidate_id,
                confidence=0.99,
                reason="当前可信候选",
            )

    class Coordinator:
        async def coordinate(self, command):
            intents.append(command.intent)
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id="capture-detail-probe"),
                interaction=SimpleNamespace(
                    execution_outcome="succeeded",
                    status="succeeded",
                    action_execution_id="actual-detail-action-failure",
                ),
            )

    class CaptureUrlReader:
        async def read_url(self, *, capture_id):
            assert capture_id == "capture-detail-probe"
            if failing_stage == "read_url":
                raise RuntimeError("read url failed")
            return "https://example.test/video/1"

    class Readiness:
        async def prepare(self, *, source_capture_id, page_role):
            assert source_capture_id == "capture-detail-probe"
            assert page_role == "detail"
            if failing_stage == "readiness":
                raise RuntimeError("readiness failed")
            return WebPageReadinessResult(
                status="ready",
                ready_capture_id="capture-detail-ready",
                dismissed_layers=(),
                evidence_capture_ids=(
                    "capture-detail-probe",
                    "capture-detail-ready",
                ),
            )

    class ReadyCaptureReader:
        async def read(self, *, capture_id, page_role):
            assert capture_id == "capture-detail-ready"
            assert page_role == "detail"
            if failing_stage == "ready_capture":
                raise RuntimeError("ready capture failed")
            return WebReadyPageCapture(
                capture_id=capture_id,
                interaction_facts=_trusted_detail_facts(f"{ORIGIN}/video/1"),
                web_capture_stage="web_page_ready_baseline",
                screenshot_artifact_id="detail:screenshot",
                dom_artifact_id="detail:dom",
                visible_text_artifact_id="detail:text",
                page_role="detail",
                structure_facts=WebPageStructureFacts(
                    navigation_element_count=0,
                    repeated_link_count=0,
                    video_element_count=1,
                    single_subject_element_count=1,
                    dom_tokens=("video", "player"),
                ),
            )

    class Classifier:
        async def classify(self, capture):
            del capture
            if failing_stage == "classifier":
                raise RuntimeError("classifier failed")
            return WebPageAssessment(
                page_type="video_detail",
                confidence=0.99,
                evidence=("详情页",),
            )

    result = await WebListDetailNavigator(
        selector=Selector(),
        coordinator=Coordinator(),
        readiness=Readiness(),
        ready_capture_reader=ReadyCaptureReader(),
        classifier=Classifier(),
        capture_url_reader=CaptureUrlReader(),
        budget_ledger=DetailBudgetLedger(),
    ).capture_one(
        WebListDetailEvidence(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            ready_capture_id="capture-list",
            screenshot_artifact_id="list:screenshot",
            dom_artifact_id="list:dom",
            origin=ORIGIN,
            current_url=f"{ORIGIN}/list",
            ui_snapshot=snapshot,
            deadline=datetime.now(UTC) + timedelta(minutes=2),
        ),
        list_page_type="video_list",
        command=_detail_command(),
        entry=_detail_entry(),
    )

    assert result.status is WebMenuTerminalStatus.NAVIGATION_FAILED
    assert result.error_code == expected_error_code
    assert result.diagnostic_type == "RuntimeError"
    assert result.diagnostic_message == expected_diagnostic
    assert "failed" not in result.diagnostic_message
    assert result.detail_action_count == 1
    assert result.probe_capture_id == "capture-detail-probe"
    assert len(intents) == 1


@pytest.mark.asyncio
async def test_retry_existing_detail_reservation_never_repeats_action_execution() -> None:
    snapshot = _snapshot(_element())
    candidate = detail_candidates_from_ready_snapshot(
        snapshot,
        list_page_type="video_list",
        origin=ORIGIN,
    )[0]
    entry = WebSiteMenuEntry(
        entry_id="menu-entry-1",
        menu_path=("视频",),
        href="/list",
        canonical_url=f"{ORIGIN}/list",
        decision=WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY,
        is_same_origin=True,
    )
    command = RunWebWorkflowCommand(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        session_owner="web-v2-workflow",
        target_url=ORIGIN,
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )

    class Selector:
        async def select_detail_candidate(self, **kwargs):
            del kwargs
            return WebDetailSelectionAssessment(
                selected_candidate_id=candidate.candidate_id,
                confidence=0.99,
                reason="当前可信候选",
            )

    class Ledger:
        reserve_count = 0

        async def reserve_detail(self, **kwargs):
            del kwargs
            self.reserve_count += 1
            return WebDetailActionReservation(
                site_id="example",
                menu_entry_id=entry.entry_id,
                intent_id="web-detail:run-1:example:menu-entry-1",
                already_reserved=self.reserve_count > 1,
            )

    class Coordinator:
        coordinate_count = 0

        async def coordinate(self, coordination_command):
            del coordination_command
            self.coordinate_count += 1
            raise CaptureCoordinationError(
                stage=CaptureCoordinationStage.AFTER_CAPTURE,
                error_code="CAPTURE_AFTER_FAILED",
                message="详情动作已执行但后采集失败",
                interaction=SimpleNamespace(
                    execution_outcome="succeeded",
                    status="succeeded",
                    action_execution_id="actual-detail-action-error",
                ),
                after_capture=SimpleNamespace(capture_id="capture-detail-after-error"),
            )

    class NeverCalled:
        async def prepare(self, **kwargs):
            raise AssertionError(kwargs)

        async def read(self, **kwargs):
            raise AssertionError(kwargs)

        async def classify(self, *args, **kwargs):
            raise AssertionError((args, kwargs))

        async def read_url(self, **kwargs):
            raise AssertionError(kwargs)

    evidence = WebListDetailEvidence(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        ready_capture_id="capture-list",
        screenshot_artifact_id="list:screenshot",
        dom_artifact_id="list:dom",
        origin=ORIGIN,
        current_url=f"{ORIGIN}/list",
        ui_snapshot=snapshot,
        deadline=command.deadline,
    )
    ledger = Ledger()
    coordinator = Coordinator()
    navigator = WebListDetailNavigator(
        selector=Selector(),
        coordinator=coordinator,
        readiness=NeverCalled(),
        ready_capture_reader=NeverCalled(),
        classifier=NeverCalled(),
        capture_url_reader=NeverCalled(),
        budget_ledger=ledger,
    )

    first = await navigator.capture_one(
        evidence,
        list_page_type="video_list",
        command=command,
        entry=entry,
    )
    second = await navigator.capture_one(
        evidence,
        list_page_type="video_list",
        command=command,
        entry=entry,
    )

    assert first.detail_action_count == 1
    assert first.detail_intent_id == "web-detail:run-1:example:menu-entry-1"
    assert first.detail_action_execution_id == "actual-detail-action-error"
    assert first.probe_capture_id == "capture-detail-after-error"
    assert second.status is WebMenuTerminalStatus.NAVIGATION_FAILED
    assert second.error_code == "WEB_BUDGET_RESERVED_NEEDS_REVIEW"
    assert second.detail_action_count == 1
    assert second.detail_action_execution_id is None
    assert ledger.reserve_count == 2
    assert coordinator.coordinate_count == 1


@pytest.mark.asyncio
async def test_none_detail_reservation_fails_closed_before_coordinate() -> None:
    candidate = detail_candidates_from_ready_snapshot(
        _snapshot(_element()),
        list_page_type="video_list",
        origin=ORIGIN,
    )[0]

    class Selector:
        async def select_detail_candidate(self, **kwargs):
            del kwargs
            return WebDetailSelectionAssessment(
                selected_candidate_id=candidate.candidate_id,
                confidence=0.99,
                reason="可信候选",
            )

    class Ledger:
        async def reserve_detail(self, **kwargs):
            del kwargs
            return None

    class Coordinator:
        async def coordinate(self, command):
            raise AssertionError(command)

    class NeverCalled:
        async def prepare(self, **kwargs):
            raise AssertionError(kwargs)

        async def read(self, **kwargs):
            raise AssertionError(kwargs)

        async def classify(self, *args, **kwargs):
            raise AssertionError((args, kwargs))

        async def read_url(self, **kwargs):
            raise AssertionError(kwargs)

    result = await WebListDetailNavigator(
        selector=Selector(),
        coordinator=Coordinator(),
        readiness=NeverCalled(),
        ready_capture_reader=NeverCalled(),
        classifier=NeverCalled(),
        capture_url_reader=NeverCalled(),
        budget_ledger=Ledger(),
    ).capture_one(
        WebListDetailEvidence(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            ready_capture_id="capture-list",
            screenshot_artifact_id="list:screenshot",
            dom_artifact_id="list:dom",
            origin=ORIGIN,
            current_url=f"{ORIGIN}/list",
            ui_snapshot=_snapshot(_element()),
            deadline=datetime.now(UTC) + timedelta(minutes=2),
        ),
        list_page_type="video_list",
        command=_detail_command(),
        entry=_detail_entry(),
    )

    assert result.status is WebMenuTerminalStatus.NAVIGATION_FAILED
    assert result.error_code == "WEB_BUDGET_RESERVED_NEEDS_REVIEW"
    assert result.detail_action_count == 1
    assert result.detail_intent_id is None
    assert result.detail_action_execution_id is None
