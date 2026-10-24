"""Web 搜索与活动辅助导航的只读、直达及边界合同。"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.interaction_runtime import (
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
)
from services.api.app.application.web_auxiliary_navigation import (
    WebAuxiliaryNavigationCandidate,
    WebAuxiliaryNavigationExplorer,
    _navigation_intent,
    discover_web_auxiliary_navigation_candidates,
    web_snapshot_has_open_search_surface,
)
from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.browser import page_capture as page_capture_module
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)
from services.api.app.infrastructure.browser.ui_snapshot_reader import (
    PostgresTrustedUiSnapshotReader,
)

ORIGIN = "https://example.test/home"


def _element(**values):
    return {
        "selector": values.pop("selector", "#target"),
        "tag": values.pop("tag", "a"),
        "text": values.pop("text", ""),
        "visible": values.pop("visible", True),
        "interactive": values.pop("interactive", True),
        "disabled": values.pop("disabled", False),
        **values,
    }


def _snapshot(*elements, capture_id="ready", url=ORIGIN):
    return {
        "schema_version": "ui-snapshot.v2",
        "capture_id": capture_id,
        "url": url,
        "structure_signature": "auxiliary-source-v1",
        "elements": list(elements),
    }


def test_discovers_only_href_backed_search_and_activity_candidates() -> None:
    snapshot = _snapshot(
        _element(selector="#search", text="搜索", href="/search"),
        _element(
            selector="#hidden-search",
            text="搜索入口",
            href="/hidden-search",
            visible=False,
            navigation_confidence=0.7,
        ),
        _element(selector="#search-button", tag="button", text="搜索"),
        _element(
            selector="#disabled-search",
            text="搜索",
            href="/disabled-search",
            aria_disabled="true",
        ),
        _element(selector="#activity", text="限时活动", href="/campaign/summer"),
        _element(
            selector="#weak-activity",
            text="专题活动",
            href="/campaign/weak",
            navigation_confidence=0.69,
        ),
        _element(selector="#activity-button", tag="button", text="立即参与"),
        _element(
            selector="#promo",
            text="专题推广",
            href="https://promo.example.net/event",
            is_advertisement=True,
        ),
        _element(selector="#download", text="活动下载", href="/download/app"),
        _element(selector="#ordinary", text="视频频道", href="/videos"),
    )

    candidates = discover_web_auxiliary_navigation_candidates(snapshot, origin=ORIGIN)

    assert [(item.kind, item.url) for item in candidates] == [
        ("search_href", "https://example.test/search"),
        ("search_href", "https://example.test/hidden-search"),
        ("activity_href", "https://example.test/campaign/summer"),
        ("external_promotion", "https://promo.example.net/event"),
    ]
    assert all(item.source_capture_id == "ready" for item in candidates)
    assert all(item.confidence >= 0.7 for item in candidates)


def test_natural_search_requires_visible_input_and_trigger_or_results() -> None:
    assert web_snapshot_has_open_search_surface(
        _snapshot(
            _element(
                tag="input",
                selector="#query",
                type="search",
                placeholder="搜索",
                parent_selector="#search-panel",
                ancestor_class_tokens=["search-panel", "content-area"],
            ),
            _element(
                tag="button",
                selector="#trigger",
                text="搜索",
                parent_selector="#search-panel",
                ancestor_class_tokens=["search-panel", "content-area"],
            ),
        )
    )
    assert web_snapshot_has_open_search_surface(
        _snapshot(
            _element(tag="input", selector="#query", role="searchbox"),
            _element(tag="section", selector="#results", class_tokens=["search-results"]),
        )
    )


def test_persistent_header_search_form_is_not_an_open_search_page() -> None:
    snapshot = _snapshot(
        _element(
            tag="input",
            selector="#header-query",
            type="search",
            placeholder="搜索",
            parent_selector="#header-search",
            ancestor_class_tokens=["header-search", "site-header"],
        ),
        _element(
            tag="button",
            selector="#header-trigger",
            text="搜索",
            parent_selector="#header-search",
            ancestor_class_tokens=["header-search", "site-header"],
        ),
        _element(tag="main", selector="#home-content", text="推荐内容"),
    )

    assert not web_snapshot_has_open_search_surface(snapshot)


@pytest.mark.asyncio
async def test_live_web_collector_recognizes_body_search_but_not_header_search() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    body_html = """
    <style>
      form { width: 480px; height: 80px; }
      input, button { width: 180px; height: 40px; }
    </style>
    <main class="content-area">
      <form id="content-search" class="search-panel query-panel">
        <input type="search" placeholder="搜索内容">
        <button type="button">搜索</button>
      </form>
    </main>
    """
    header_html = """
    <style>
      header, form { width: 480px; height: 80px; }
      input, button { width: 180px; height: 40px; }
    </style>
    <header class="site-header">
      <form id="header-search" class="search-box">
        <input type="search" placeholder="搜索内容">
        <button type="button">搜索</button>
      </form>
    </header>
    <main><h1>首页推荐</h1></main>
    """
    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except Exception:  # noqa: BLE001 - 本机可复用已安装 Chrome 通道
                browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page(viewport={"width": 1024, "height": 768})
            await page.set_content(body_html)
            body_snapshot = await page_capture_module._collect_ui_snapshot(page)
            await page.set_content(header_html)
            header_snapshot = await page_capture_module._collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境无 Chromium 时按既有集成测试约定跳过
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    body_controls = [
        element
        for element in body_snapshot["elements"]
        if element.get("tag") in {"input", "button"}
    ]
    assert len(body_controls) == 2
    assert {element.get("parent_selector") for element in body_controls} == {"#content-search"}
    assert all("search-panel" in element["ancestor_class_tokens"] for element in body_controls)
    assert all("search_container_id" not in element for element in body_controls)
    assert web_snapshot_has_open_search_surface(body_snapshot)
    assert not web_snapshot_has_open_search_surface(header_snapshot)


def test_relative_href_uses_trusted_snapshot_url_as_base() -> None:
    snapshot = _snapshot(
        _element(selector="#search", text="搜索", href="search"),
        url="https://example.test/section/index",
    )

    candidates = discover_web_auxiliary_navigation_candidates(snapshot, origin=ORIGIN)

    assert [item.url for item in candidates] == ["https://example.test/section/search"]


def test_candidate_discovery_rejects_snapshot_from_another_site() -> None:
    snapshot = _snapshot(
        _element(selector="#search", text="搜索", href="https://example.test/search"),
        url="https://attacker.test/source",
    )

    assert discover_web_auxiliary_navigation_candidates(snapshot, origin=ORIGIN) == ()
    assert not web_snapshot_has_open_search_surface(
        _snapshot(_element(tag="button", selector="#trigger", text="搜索"))
    )
    assert not web_snapshot_has_open_search_surface(
        _snapshot(_element(tag="input", selector="#query", type="search", placeholder="搜索"))
    )
    assert not web_snapshot_has_open_search_surface(
        _snapshot(
            _element(
                tag="input",
                selector="#query",
                type="search",
                placeholder="搜索",
                visible=False,
            ),
            _element(tag="button", selector="#trigger", text="搜索"),
        )
    )


class _SnapshotReader:
    def __init__(self, snapshots, *, before_action_bindings=None):
        self.snapshots = snapshots
        self.before_action_bindings = before_action_bindings or {}

    async def read_snapshot(self, *, capture_id):
        return self.snapshots[capture_id]

    async def read_url(self, *, capture_id):
        return self.snapshots[capture_id]["url"]

    async def read_ui_snapshot(self, *, task_id, page_capture_id):
        assert task_id == "task-1"
        return self.snapshots.get(page_capture_id)

    async def read_web_before_action_binding(self, *, task_id, page_capture_id):
        assert task_id == "task-1"
        return self.before_action_bindings.get(page_capture_id)


def _before_action_binding(
    *,
    discovery_capture_id="capture-discovery",
    before_capture_id="capture-before-action",
    agent_run_id="run-random",
    final_url="https://variable-layout.test/landing",
    parent_final_url="https://variable-layout.test/landing",
    candidate_id="candidate-random",
    session_id="web:run-random",
    semantic_target="进入搜索页面",
    origin="https://variable-layout.test/landing",
    destination_url="https://variable-layout.test/find",
    surface_status="active",
):
    return {
        "capture_id": before_capture_id,
        "task_id": "task-1",
        "agent_run_id": agent_run_id,
        "surface_kind": "web",
        "session_id": session_id,
        "parent_capture_id": discovery_capture_id,
        "action_execution_id": "action-random",
        "capture_role": "before_action",
        "capture_status": "captured",
        "session_command_sequence": 7,
        "final_url": final_url,
        "parent_agent_run_id": agent_run_id,
        "parent_surface_kind": "web",
        "parent_session_id": session_id,
        "parent_capture_status": "captured",
        "parent_final_url": parent_final_url,
        "action_task_id": "task-1",
        "action_session_id": session_id,
        "action_requested_source_capture_id": discovery_capture_id,
        "action_source_capture_id": before_capture_id,
        "action_command_sequence": 7,
        "action_finalization_epoch": 3,
        "action_status": "executing",
        "action_requested_url": destination_url,
        "action_intent": {
            "task_id": "task-1",
            "session_id": session_id,
            "source_capture_id": discovery_capture_id,
            "action_type": "navigate",
            "semantic_target": semantic_target,
            "parameters": {
                "target_mode": "web_search_discovery_candidate",
                "web_auxiliary_candidate_id": candidate_id,
                "web_auxiliary_source_capture_id": discovery_capture_id,
                "web_auxiliary_candidate_source_capture_id": discovery_capture_id,
                "web_auxiliary_origin": origin,
                "web_auxiliary_kind": "search_href",
                "destination_url": destination_url,
            },
        },
        "surface_current_capture_id": before_capture_id,
        "surface_command_sequence": 7,
        "surface_finalization_epoch": 3,
        "surface_status": surface_status,
    }


@pytest.mark.asyncio
async def test_postgres_snapshot_reader_reads_formal_before_action_binding() -> None:
    task_id = "11111111-1111-4111-8111-111111111111"
    capture_id = "22222222-2222-4222-8222-222222222222"
    row = {
        **_before_action_binding(before_capture_id=capture_id),
        "capture_id": capture_id,
        "task_id": task_id,
    }

    class Connection:
        query = ""

        async def fetchrow(self, query, *args):
            self.query = query
            assert args == (task_id, capture_id)
            return row

    class Acquire:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    connection = Connection()
    binding = await PostgresTrustedUiSnapshotReader(
        pool=Pool(),
        artifact_store=object(),
    ).read_web_before_action_binding(
        task_id=task_id,
        page_capture_id=capture_id,
    )

    assert binding == row
    assert "qc_action_execution" in connection.query
    assert "qc_surface_session" in connection.query
    assert "parent_capture" in connection.query


@pytest.mark.asyncio
async def test_postgres_snapshot_reader_decodes_asyncpg_json_text_action_intent() -> None:
    """生产连接把 jsonb 返回为文本时，详情与辅助入口仍须读取正式动作意图。"""
    import json

    task_id = "11111111-1111-4111-8111-111111111111"
    capture_id = "22222222-2222-4222-8222-222222222222"
    expected_intent = _before_action_binding()["action_intent"]
    row = {
        **_before_action_binding(before_capture_id=capture_id),
        "capture_id": capture_id,
        "task_id": task_id,
        "action_intent": json.dumps(expected_intent, ensure_ascii=False),
    }

    class Connection:
        async def fetchrow(self, _query, *_args):
            return row

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    binding = await PostgresTrustedUiSnapshotReader(
        pool=Pool(),
        artifact_store=object(),
    ).read_web_before_action_binding(
        task_id=task_id,
        page_capture_id=capture_id,
    )

    assert binding["action_intent"] == expected_intent


class _Coordinator:
    def __init__(self):
        self.commands = []

    async def coordinate(self, command):
        self.commands.append(command)
        return SimpleNamespace(after_capture=SimpleNamespace(capture_id="probe-after"))


class _Readiness:
    def __init__(self):
        self.calls = []

    async def prepare(self, *, source_capture_id, page_role):
        self.calls.append((source_capture_id, page_role))
        assert source_capture_id == "probe-after"
        assert page_role == "menu"
        return SimpleNamespace(
            status="stable",
            ready_capture_id="ready-after",
            evidence_capture_ids=("probe-after", "ready-after"),
        )


class _ReadyReader:
    async def read(self, *, capture_id, page_role):
        return SimpleNamespace(capture_id=capture_id, page_role=page_role, url=ORIGIN)


class _Classifier:
    def __init__(self, page_type):
        self.page_type = page_type
        self.calls = []

    async def classify(self, capture):
        self.calls.append(capture)
        return SimpleNamespace(page_type=self.page_type, raw_page_type=self.page_type)


class _Claims:
    def __init__(self):
        self.claimed = []
        self.finished = []
        self._keys = set()
        self.finish_error = None

    async def claim(self, **kwargs):
        key = (kwargs["phase_key"], kwargs["source_capture_id"], kwargs["candidate_id"])
        self.claimed.append(kwargs)
        if key in self._keys:
            return None
        self._keys.add(key)
        return "phase-1"

    async def finish(self, **kwargs):
        self.finished.append(kwargs)
        if self.finish_error is not None:
            raise self.finish_error


def _command():
    return RunWebWorkflowCommand(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        session_owner="web-v2-workflow",
        target_url=ORIGIN,
        browser_profile_id="desktop-default",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


def _explorer(snapshot, *, page_type, claims=None, ad_scheduler=None, final_url=None):
    coordinator = _Coordinator()
    classifier = _Classifier(page_type)
    snapshot_reader = _SnapshotReader(
        {
            "ready": snapshot,
            "probe-after": {
                **snapshot,
                "capture_id": "probe-after",
                "url": final_url or "https://example.test/landed",
            },
            "ready-after": {
                **snapshot,
                "capture_id": "ready-after",
                "url": final_url or "https://example.test/landed",
            },
        }
    )
    readiness = _Readiness()
    explorer = WebAuxiliaryNavigationExplorer(
        coordinator=coordinator,
        snapshot_reader=snapshot_reader,
        capture_url_reader=snapshot_reader,
        readiness=readiness,
        ready_capture_reader=_ReadyReader(),
        classifier=classifier,
        claims=claims,
        ad_landing_scheduler=ad_scheduler,
    )
    return explorer, coordinator, classifier, readiness


@pytest.mark.asyncio
async def test_search_href_uses_navigate_without_click_input_or_submit() -> None:
    snapshot = _snapshot(_element(selector="#search", text="搜索", href="/search"))
    claims = _Claims()
    scheduler = _AdScheduler()
    explorer, coordinator, _classifier, _readiness = _explorer(
        snapshot, page_type="search_page", claims=claims, ad_scheduler=scheduler
    )

    result = await explorer.explore(_command(), source_capture_id="ready")

    intent = coordinator.commands[0].intent
    assert intent.action_type is InteractionOperation.NAVIGATE
    assert intent.parameters["destination_url"] == "https://example.test/search"
    assert intent.parameters["target_mode"] == "web_search_discovery_candidate"
    assert intent.parameters["web_auxiliary_phase_key"] == "web_search_discovery"
    assert "selector" not in intent.parameters
    assert "input_text" not in intent.parameters
    assert "submit_selector" not in intent.parameters
    assert len(coordinator.commands) == 1
    assert result.page_type == "search_page"
    assert result.ad_landing_evidence == {"status": "observed", "capture_id": "ad-capture"}
    assert len(scheduler.pages) == 1
    assert scheduler.pages[0].page_role == "search"
    assert scheduler.pages[0].ready_capture_id == "ready-after"
    assert claims.finished[0]["reason_code"] == "WEB_AUXILIARY_CLASSIFIED"


@pytest.mark.asyncio
@pytest.mark.parametrize("locator_like_id", [".search", "css=search"])
async def test_stage_search_href_uses_semantic_target_for_locator_like_label(
    locator_like_id,
) -> None:
    snapshot = _snapshot(_element(text="", id=locator_like_id, href="/search"))
    (candidate,) = discover_web_auxiliary_navigation_candidates(snapshot, origin=ORIGIN)
    explorer, coordinator, _classifier, _readiness = _explorer(
        snapshot,
        page_type="search_page",
        claims=_Claims(),
        final_url="https://example.test/search",
    )

    capture_id = await explorer.stage_navigate(
        _command(),
        source_capture_id="ready",
        candidate_id=candidate.candidate_id,
        requested_url=candidate.url,
    )

    assert capture_id == "probe-after"
    intent = coordinator.commands[0].intent
    assert intent.semantic_target == "进入搜索页面"
    assert intent.parameters["web_auxiliary_candidate_id"] == candidate.candidate_id
    assert "selector" not in intent.parameters


@pytest.mark.parametrize("kind", ["external_promotion", "unknown_kind"])
def test_navigation_intent_rejects_non_navigable_auxiliary_kind(kind) -> None:
    candidate = WebAuxiliaryNavigationCandidate(
        candidate_id="web-auxiliary:synthetic",
        source_capture_id="ready",
        kind=kind,
        label="活动入口",
        selector=None,
        url="https://example.test/campaign",
        confidence=1.0,
    )

    with pytest.raises(ValueError, match="WEB_AUXILIARY_CANDIDATE_KIND_NOT_NAVIGABLE"):
        _navigation_intent(
            _command(),
            source_capture_id="ready",
            candidate=candidate,
            phase_key="web_activity_navigation",
        )


@pytest.mark.asyncio
async def test_activity_href_schedules_one_ad_attempt_from_its_ready_capture() -> None:
    snapshot = _snapshot(_element(selector="#activity", text="专题活动", href="/campaign/topic"))
    scheduler = _AdScheduler()
    explorer, coordinator, _classifier, _readiness = _explorer(
        snapshot,
        page_type="activity",
        claims=_Claims(),
        ad_scheduler=scheduler,
    )

    result = await explorer.explore(_command(), source_capture_id="ready")

    assert result.status == "classified"
    assert coordinator.commands[0].intent.semantic_target == "进入活动页面"
    assert result.ad_landing_evidence == {"status": "observed", "capture_id": "ad-capture"}
    assert len(scheduler.pages) == 1
    assert scheduler.pages[0].page_role == "activity"
    assert scheduler.pages[0].ready_capture_id == "ready-after"


@pytest.mark.asyncio
async def test_search_and_activity_hrefs_are_each_navigated_once_without_click_or_input() -> None:
    snapshot = _snapshot(
        _element(selector="#search", text="搜索", href="/search"),
        _element(selector="#activity", text="专题活动", href="/campaign/topic"),
    )

    class Coordinator:
        def __init__(self):
            self.commands = []

        async def coordinate(self, command):
            self.commands.append(command)
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id=f"probe-{len(self.commands)}")
            )

    class Reader(_SnapshotReader):
        async def read_url(self, *, capture_id):
            return {
                "probe-1": "https://example.test/search",
                "probe-2": "https://example.test/campaign/topic",
            }[capture_id]

    class Readiness:
        async def prepare(self, *, source_capture_id, page_role):
            assert page_role == "menu"
            return SimpleNamespace(
                status="stable",
                ready_capture_id=source_capture_id.replace("probe", "ready"),
            )

    snapshots = {
        "ready": snapshot,
        # 第二类候选必须从首次导航后的新 current Capture 重新发现。
        "ready-1": _snapshot(
            _element(selector="#activity-next", text="专题活动", href="/campaign/topic"),
            capture_id="ready-1",
            url="https://example.test/search",
        ),
        "ready-2": _snapshot(
            capture_id="ready-2",
            url="https://example.test/campaign/topic",
        ),
    }
    coordinator = Coordinator()
    claims = _Claims()
    explorer = WebAuxiliaryNavigationExplorer(
        coordinator=coordinator,
        snapshot_reader=Reader(snapshots),
        capture_url_reader=Reader(snapshots),
        readiness=Readiness(),
        ready_capture_reader=_ReadyReader(),
        classifier=_Classifier("unknown"),
        claims=claims,
    )

    results = await explorer.explore_all(_command(), source_capture_id="ready")

    assert [result.phase_key for result in results] == [
        "web_search_discovery",
        "web_activity_navigation",
    ]
    intents = [item.intent for item in coordinator.commands]
    assert [intent.action_type for intent in intents] == [
        InteractionOperation.NAVIGATE,
        InteractionOperation.NAVIGATE,
    ]
    assert [intent.parameters["destination_url"] for intent in intents] == [
        "https://example.test/search",
        "https://example.test/campaign/topic",
    ]
    assert [intent.source_capture_id for intent in intents] == ["ready", "ready-1"]
    assert [
        intent.parameters["web_auxiliary_candidate_source_capture_id"] for intent in intents
    ] == [
        "ready",
        "ready-1",
    ]
    assert [claim["source_capture_id"] for claim in claims.claimed] == ["ready", "ready-1"]
    assert [claim["candidate_source_capture_id"] for claim in claims.claimed] == [
        "ready",
        "ready-1",
    ]
    assert all(
        forbidden not in intent.parameters
        for intent in intents
        for forbidden in ("selector", "input_text", "submit_selector")
    )


@pytest.mark.asyncio
async def test_failed_first_href_advances_current_to_probe_before_second_navigation() -> None:
    snapshot = _snapshot(
        _element(selector="#search", text="搜索", href="/search"),
        _element(selector="#activity", text="专题活动", href="/campaign/topic"),
    )

    class Coordinator:
        def __init__(self):
            self.commands = []

        async def coordinate(self, command):
            self.commands.append(command)
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id=f"probe-{len(self.commands)}")
            )

    class Reader(_SnapshotReader):
        async def read_url(self, *, capture_id):
            return {
                "probe-1": "https://example.test/search",
                "probe-2": "https://example.test/campaign/topic",
            }[capture_id]

    class Readiness:
        async def prepare(self, *, source_capture_id, page_role):
            assert page_role == "menu"
            if source_capture_id == "probe-1":
                return SimpleNamespace(status="failed", ready_capture_id=None)
            assert source_capture_id == "probe-2"
            return SimpleNamespace(status="stable", ready_capture_id="ready-2")

    snapshots = {
        "ready": snapshot,
        "probe-1": _snapshot(
            _element(selector="#activity-next", text="专题活动", href="/campaign/topic"),
            capture_id="probe-1",
            url="https://example.test/search",
        ),
        "ready-2": _snapshot(
            capture_id="ready-2",
            url="https://example.test/campaign/topic",
        ),
    }
    coordinator = Coordinator()
    claims = _Claims()
    explorer = WebAuxiliaryNavigationExplorer(
        coordinator=coordinator,
        snapshot_reader=Reader(snapshots),
        capture_url_reader=Reader(snapshots),
        readiness=Readiness(),
        ready_capture_reader=_ReadyReader(),
        classifier=_Classifier("activity"),
        claims=claims,
    )

    results = await explorer.explore_all(_command(), source_capture_id="ready")

    assert [(result.status, result.probe_capture_id) for result in results] == [
        ("failed", "probe-1"),
        ("classified", "probe-2"),
    ]
    assert [claim["source_capture_id"] for claim in claims.claimed] == [
        "ready",
        "probe-1",
    ]
    intents = [item.intent for item in coordinator.commands]
    assert [intent.source_capture_id for intent in intents] == ["ready", "probe-1"]
    assert [intent.action_type for intent in intents] == [
        InteractionOperation.NAVIGATE,
        InteractionOperation.NAVIGATE,
    ]
    assert all(
        forbidden not in intent.parameters
        for intent in intents
        for forbidden in ("selector", "input_text", "submit_selector")
    )


@pytest.mark.asyncio
async def test_explorer_rejects_candidate_from_different_action_source() -> None:
    source_snapshot = _snapshot(
        _element(selector="#search", text="搜索", href="/search"),
        capture_id="old-ready",
    )
    candidate = discover_web_auxiliary_navigation_candidates(
        source_snapshot,
        origin=ORIGIN,
    )[0]
    explorer, coordinator, _classifier, _readiness = _explorer(
        source_snapshot,
        page_type="search_page",
        claims=_Claims(),
    )

    result = await explorer.explore(
        _command(),
        source_capture_id="new-ready",
        candidate=candidate,
        _candidate_snapshot=source_snapshot,
    )

    assert result.status == "candidate_stale"
    assert coordinator.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_fails", (False, True))
async def test_cancellation_closes_auxiliary_claim_then_preserves_original_cancel(
    finish_fails: bool,
) -> None:
    class CancelledCoordinator(_Coordinator):
        async def coordinate(self, command):
            self.commands.append(command)
            raise asyncio.CancelledError()

    snapshot = _snapshot(_element(selector="#search", text="搜索", href="/search"))
    claims = _Claims()
    if finish_fails:
        claims.finish_error = RuntimeError("finish unavailable")
    explorer, _coordinator, _classifier, _readiness = _explorer(
        snapshot, page_type="search_page", claims=claims
    )
    explorer._coordinator = CancelledCoordinator()

    with pytest.raises(asyncio.CancelledError):
        await explorer.explore(_command(), source_capture_id="ready")

    assert claims.finished == [
        {
            "phase_id": "phase-1",
            "status": "failed",
            "reason_code": "WEB_AUXILIARY_CANCELLED",
            "result_capture_id": None,
            "page_type": None,
        }
    ]


@pytest.mark.asyncio
async def test_same_origin_href_redirecting_cross_origin_fails_before_readiness() -> None:
    snapshot = _snapshot(_element(selector="#search", text="搜索", href="/search"))
    claims = _Claims()
    explorer, coordinator, classifier, readiness = _explorer(
        snapshot,
        page_type="search_page",
        claims=claims,
        final_url="https://outside.example.net/redirected",
    )

    result = await explorer.explore(_command(), source_capture_id="ready")

    assert len(coordinator.commands) == 1
    assert readiness.calls == []
    assert classifier.calls == []
    assert result.status == "failed"
    assert result.reason_code == "WEB_NAVIGATION_CROSS_ORIGIN"
    assert result.probe_capture_id == "probe-after"
    assert claims.finished[0]["status"] == "failed"
    assert claims.finished[0]["reason_code"] == "WEB_NAVIGATION_CROSS_ORIGIN"
    assert claims.finished[0]["result_capture_id"] == "probe-after"
    assert claims.finished[0]["page_type"] is None


@pytest.mark.asyncio
async def test_failed_probe_finish_error_does_not_retry_with_generic_reason() -> None:
    class NotReady:
        async def prepare(self, *, source_capture_id, page_role):
            assert source_capture_id == "probe-after"
            assert page_role == "menu"
            return SimpleNamespace(status="failed", ready_capture_id=None)

    snapshot = _snapshot(_element(selector="#search", text="搜索", href="/search"))
    claims = _Claims()
    claims.finish_error = RuntimeError("finish rejected")
    explorer, _coordinator, _classifier, _readiness = _explorer(
        snapshot,
        page_type="search_page",
        claims=claims,
        final_url="https://example.test/search",
    )
    explorer._readiness = NotReady()

    with pytest.raises(RuntimeError, match="finish rejected"):
        await explorer.explore(_command(), source_capture_id="ready")

    assert len(claims.finished) == 1
    assert claims.finished[0]["reason_code"] == "WEB_AUXILIARY_NOT_READY"
    assert claims.finished[0]["result_capture_id"] == "probe-after"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "expected_reason"),
    (
        ("safety", "WEB_NAVIGATION_CROSS_ORIGIN"),
        ("final_url", "WEB_AUXILIARY_FINAL_URL_UNAVAILABLE"),
    ),
)
async def test_expected_probe_failure_finish_error_never_retries_generic_fallback(
    failure_kind: str,
    expected_reason: str,
) -> None:
    class UnavailableUrlReader:
        async def read_url(self, *, capture_id):
            assert capture_id == "probe-after"
            raise ValueError("capture URL unavailable")

    snapshot = _snapshot(_element(selector="#search", text="搜索", href="/search"))
    claims = _Claims()
    claims.finish_error = RuntimeError("finish rejected")
    explorer, _coordinator, _classifier, _readiness = _explorer(
        snapshot,
        page_type="search_page",
        claims=claims,
        final_url=(
            "https://outside.example.net/redirected"
            if failure_kind == "safety"
            else "https://example.test/search"
        ),
    )
    if failure_kind == "final_url":
        explorer._capture_url_reader = UnavailableUrlReader()

    with pytest.raises(RuntimeError, match="finish rejected"):
        await explorer.explore(_command(), source_capture_id="ready")

    assert len(claims.finished) == 1
    assert claims.finished[0]["reason_code"] == expected_reason
    assert claims.finished[0]["result_capture_id"] == "probe-after"
    assert claims.finished[0]["page_type"] is None


@pytest.mark.asyncio
async def test_naturally_open_search_is_classified_without_any_action() -> None:
    snapshot = _snapshot(
        _element(tag="input", selector="#query", role="searchbox"),
        _element(tag="section", selector="#results", class_tokens=["search-results"]),
    )
    explorer, coordinator, classifier, _readiness = _explorer(snapshot, page_type="search_page")

    result = await explorer.explore(_command(), source_capture_id="ready")

    assert coordinator.commands == []
    assert len(classifier.calls) == 1
    assert result.status == "classified_existing_search_surface"
    assert result.page_type == "search_page"


@pytest.mark.asyncio
async def test_same_origin_activity_href_uses_navigate_and_unknown_is_not_retried() -> None:
    snapshot = _snapshot(_element(selector="#activity", text="专题活动", href="/campaign/topic"))
    claims = _Claims()
    explorer, coordinator, _classifier, _readiness = _explorer(
        snapshot, page_type="unknown", claims=claims
    )

    first = await explorer.explore(_command(), source_capture_id="ready")
    second = await explorer.explore(_command(), source_capture_id="ready")

    assert coordinator.commands[0].intent.action_type is InteractionOperation.NAVIGATE
    assert coordinator.commands[0].intent.parameters["destination_url"] == (
        "https://example.test/campaign/topic"
    )
    assert coordinator.commands[0].intent.parameters["web_auxiliary_phase_key"] == (
        "web_activity_navigation"
    )
    assert len(coordinator.commands) == 1
    assert first.status == "failed"
    assert first.page_type == "unknown"
    assert first.reason_code == "WEB_AUXILIARY_PAGE_TYPE_MISMATCH"
    assert first.ready_capture_id == "ready-after"
    assert claims.finished[0]["status"] == "failed"
    assert claims.finished[0]["result_capture_id"] == "ready-after"
    assert claims.finished[0]["page_type"] is None
    assert second.status == "already_attempted"


@pytest.mark.asyncio
async def test_href_less_search_and_activity_controls_are_never_executed() -> None:
    snapshot = _snapshot(
        _element(tag="button", selector="#search", text="搜索"),
        _element(tag="button", selector="#activity", text="立即参与"),
    )
    explorer, coordinator, classifier, _readiness = _explorer(snapshot, page_type="unknown")

    result = await explorer.explore(_command(), source_capture_id="ready")

    assert result.status == "no_candidate"
    assert coordinator.commands == []
    assert classifier.calls == []


@pytest.mark.asyncio
async def test_href_navigation_fails_closed_without_durable_phase_claims() -> None:
    snapshot = _snapshot(_element(selector="#search", text="搜索", href="/search"))
    explorer, coordinator, classifier, _readiness = _explorer(snapshot, page_type="search_page")

    result = await explorer.explore(_command(), source_capture_id="ready")

    assert result.status == "phase_claim_unavailable"
    assert result.reason_code == "WEB_AUXILIARY_PHASE_CLAIMS_REQUIRED"
    assert coordinator.commands == []
    assert classifier.calls == []


class _AdScheduler:
    def __init__(self):
        self.pages = []

    async def attempt(self, page):
        self.pages.append(page)
        return SimpleNamespace(
            status="observed",
            reason_code=None,
            capture_id="ad-capture",
            to_dict=lambda: {"status": "observed", "capture_id": "ad-capture"},
        )


@pytest.mark.asyncio
async def test_cross_origin_promotion_is_deferred_to_the_stage_ad_path() -> None:
    snapshot = _snapshot(
        _element(
            selector="#promo",
            text="限时活动推广",
            href="https://promo.example.net/event",
            is_advertisement=True,
            class_tokens=["promotion-banner"],
        )
    )
    scheduler = _AdScheduler()
    claims = _Claims()
    explorer, coordinator, _classifier, _readiness = _explorer(
        snapshot,
        page_type="activity",
        claims=claims,
        ad_scheduler=scheduler,
    )

    result = await explorer.explore(_command(), source_capture_id="ready")

    assert result.status == "external_promotion_deferred"
    assert result.reason_code == "WEB_AD_STAGE_PATH_REQUIRED"
    assert coordinator.commands == []
    assert scheduler.pages == []
    assert claims.claimed == []
    assert claims.finished == []


@pytest.mark.asyncio
async def test_semantic_resolver_rebuilds_href_candidate_from_source_snapshot() -> None:
    snapshot = _snapshot(
        _element(selector="#search", text="搜索", href="/search"),
        capture_id="ready",
    )
    before = _snapshot(capture_id="ready-before")
    candidate = discover_web_auxiliary_navigation_candidates(snapshot, origin=ORIGIN)[0]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=_SnapshotReader(
            {"ready": snapshot, "ready-before": before},
            before_action_bindings={
                "ready-before": _before_action_binding(
                    discovery_capture_id="ready",
                    before_capture_id="ready-before",
                    agent_run_id="run-1",
                    final_url=ORIGIN,
                    parent_final_url=ORIGIN,
                    candidate_id=candidate.candidate_id,
                    session_id="web:run-1",
                    semantic_target="进入搜索页面",
                    origin=ORIGIN,
                    destination_url="https://example.test/search",
                )
            },
        )
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-1",
            source_capture_id="ready-before",
            action_type=InteractionOperation.NAVIGATE,
            semantic_target="进入搜索页面",
            parameters={
                "target_mode": "web_search_discovery_candidate",
                "web_auxiliary_candidate_id": (candidate.candidate_id),
                "web_auxiliary_source_capture_id": "ready",
                "web_auxiliary_candidate_source_capture_id": "ready",
                "web_auxiliary_before_action_capture_id": "ready-before",
                "web_auxiliary_origin": ORIGIN,
                "web_auxiliary_kind": "search_href",
                "destination_url": "https://example.test/search",
            },
        )
    )

    assert target.destination_url == "https://example.test/search"
    assert target.target_source == "web_auxiliary_navigation_candidate"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface_status", ["active", "draining"])
async def test_semantic_resolver_keeps_discovery_identity_across_fresh_before_capture(
    surface_status,
) -> None:
    origin = "https://variable-layout.test/landing"
    discovery = _snapshot(
        _element(selector="a:nth-child(3)", text="站内搜索", href="/find"),
        capture_id="capture-discovery",
        url=origin,
    )
    before_action = _snapshot(
        _element(selector="nav a:nth-child(7)", text="结构已变化", href="/other"),
        capture_id="capture-before-action",
        url=origin,
    )
    candidate = discover_web_auxiliary_navigation_candidates(
        discovery,
        origin=origin,
    )[0]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=_SnapshotReader(
            {
                "capture-discovery": discovery,
                "capture-before-action": before_action,
            },
            before_action_bindings={
                "capture-before-action": _before_action_binding(
                    candidate_id=candidate.candidate_id,
                    surface_status=surface_status,
                )
            },
        )
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-random",
            source_capture_id="capture-before-action",
            action_type=InteractionOperation.NAVIGATE,
            semantic_target="进入搜索页面",
            parameters={
                "target_mode": "web_search_discovery_candidate",
                "web_auxiliary_candidate_id": candidate.candidate_id,
                "web_auxiliary_source_capture_id": "capture-discovery",
                "web_auxiliary_candidate_source_capture_id": "capture-discovery",
                "web_auxiliary_before_action_capture_id": "capture-before-action",
                "web_auxiliary_origin": origin,
                "web_auxiliary_kind": "search_href",
                "destination_url": "https://variable-layout.test/find",
            },
        )
    )

    assert target.destination_url == "https://variable-layout.test/find"
    assert target.target_signature == candidate.candidate_id


@pytest.mark.asyncio
async def test_semantic_resolver_accepts_discovery_cursor_until_command_release() -> None:
    origin = "https://runtime-order.test/landing"
    discovery = _snapshot(
        _element(selector="a:nth-child(5)", text="站内搜索", href="/find"),
        capture_id="capture-discovery",
        url=origin,
    )
    before_action = _snapshot(
        capture_id="capture-before-action",
        url=origin,
    )
    candidate = discover_web_auxiliary_navigation_candidates(
        discovery,
        origin=origin,
    )[0]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=_SnapshotReader(
            {
                "capture-discovery": discovery,
                "capture-before-action": before_action,
            },
            before_action_bindings={
                "capture-before-action": _before_action_binding(
                    final_url=origin,
                    parent_final_url=origin,
                    candidate_id=candidate.candidate_id,
                    origin=origin,
                    destination_url="https://runtime-order.test/find",
                    surface_status="active",
                )
                | {"surface_current_capture_id": "capture-discovery"}
            },
        )
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-random",
            source_capture_id="capture-before-action",
            action_type=InteractionOperation.NAVIGATE,
            semantic_target="进入搜索页面",
            parameters={
                "target_mode": "web_search_discovery_candidate",
                "web_auxiliary_candidate_id": candidate.candidate_id,
                "web_auxiliary_source_capture_id": "capture-discovery",
                "web_auxiliary_candidate_source_capture_id": "capture-discovery",
                "web_auxiliary_before_action_capture_id": "capture-before-action",
                "web_auxiliary_origin": origin,
                "web_auxiliary_kind": "search_href",
                "destination_url": "https://runtime-order.test/find",
            },
        )
    )

    assert target.destination_url == "https://runtime-order.test/find"
    assert target.target_signature == candidate.candidate_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "before_snapshot", "binding_changes"),
    [
        ("missing_before_capture", None, {}),
        (
            "snapshot_capture_id_drift",
            _snapshot(
                capture_id="capture-drifted",
                url="https://variable-layout.test/landing",
            ),
            {},
        ),
        (
            "cross_origin_before_capture",
            _snapshot(
                capture_id="capture-before-action",
                url="https://foreign-origin.test/landing",
            ),
            {"final_url": "https://foreign-origin.test/landing"},
        ),
        (
            "wrong_parent",
            _snapshot(
                capture_id="capture-before-action",
                url="https://variable-layout.test/landing",
            ),
            {"parent_capture_id": "capture-other"},
        ),
        (
            "wrong_run",
            _snapshot(
                capture_id="capture-before-action",
                url="https://variable-layout.test/landing",
            ),
            {"parent_agent_run_id": "run-other"},
        ),
        (
            "binding_run_detached_from_web_session",
            _snapshot(
                capture_id="capture-before-action",
                url="https://variable-layout.test/landing",
            ),
            {
                "agent_run_id": "run-other",
                "parent_agent_run_id": "run-other",
            },
        ),
        *(
            (
                f"surface_{surface_status}",
                _snapshot(
                    capture_id="capture-before-action",
                    url="https://variable-layout.test/landing",
                ),
                {"surface_status": surface_status},
            )
            for surface_status in ("starting", "closing", "closed", "lost", "failed")
        ),
        (
            "wrong_role",
            _snapshot(
                capture_id="capture-before-action",
                url="https://variable-layout.test/landing",
            ),
            {"capture_role": "observation"},
        ),
        (
            "wrong_action_source",
            _snapshot(
                capture_id="capture-before-action",
                url="https://variable-layout.test/landing",
            ),
            {"action_source_capture_id": "capture-other"},
        ),
        (
            "wrong_epoch",
            _snapshot(
                capture_id="capture-before-action",
                url="https://variable-layout.test/landing",
            ),
            {"surface_finalization_epoch": 4},
        ),
        (
            "wrong_sequence",
            _snapshot(
                capture_id="capture-before-action",
                url="https://variable-layout.test/landing",
            ),
            {"action_command_sequence": 8},
        ),
        (
            "not_current",
            _snapshot(
                capture_id="capture-before-action",
                url="https://variable-layout.test/landing",
            ),
            {"surface_current_capture_id": "capture-other"},
        ),
    ],
)
async def test_semantic_resolver_rejects_untrusted_fresh_before_capture_chain(
    case,
    before_snapshot,
    binding_changes,
) -> None:
    del case
    origin = "https://variable-layout.test/landing"
    discovery = _snapshot(
        _element(selector="main a:nth-child(11)", text="站内搜索", href="/find"),
        capture_id="capture-discovery",
        url=origin,
    )
    candidate = discover_web_auxiliary_navigation_candidates(
        discovery,
        origin=origin,
    )[0]
    snapshots = {"capture-discovery": discovery}
    bindings = {}
    if before_snapshot is not None:
        snapshots["capture-before-action"] = before_snapshot
        bindings["capture-before-action"] = {
            **_before_action_binding(candidate_id=candidate.candidate_id),
            **binding_changes,
        }
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=_SnapshotReader(
            snapshots,
            before_action_bindings=bindings,
        )
    )

    with pytest.raises(SemanticTargetResolutionError) as exc_info:
        await resolver.resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="web:run-random",
                source_capture_id="capture-before-action",
                action_type=InteractionOperation.NAVIGATE,
                semantic_target="进入搜索页面",
                parameters={
                    "target_mode": "web_search_discovery_candidate",
                    "web_auxiliary_candidate_id": candidate.candidate_id,
                    "web_auxiliary_source_capture_id": "capture-discovery",
                    "web_auxiliary_candidate_source_capture_id": ("capture-discovery"),
                    "web_auxiliary_before_action_capture_id": ("capture-before-action"),
                    "web_auxiliary_origin": origin,
                    "web_auxiliary_kind": "search_href",
                    "destination_url": "https://variable-layout.test/find",
                },
            )
        )

    assert exc_info.value.error_code == "WEB_AUXILIARY_CANDIDATE_SOURCE_MISMATCH"


@pytest.mark.asyncio
async def test_semantic_resolver_rejects_href_harvested_from_prior_capture() -> None:
    snapshot = _snapshot(_element(selector="#activity", text="专题活动", href="/campaign"))
    candidate = discover_web_auxiliary_navigation_candidates(snapshot, origin=ORIGIN)[0]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=_SnapshotReader(
            {
                "ready": snapshot,
                "current-after-search": _snapshot(
                    url="https://example.test/search",
                    capture_id="current-after-search",
                ),
            }
        )
    )

    with pytest.raises(SemanticTargetResolutionError) as exc_info:
        await resolver.resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="web:run-1",
                source_capture_id="current-after-search",
                action_type=InteractionOperation.NAVIGATE,
                semantic_target="进入活动页面",
                parameters={
                    "target_mode": "web_search_discovery_candidate",
                    "web_auxiliary_candidate_id": candidate.candidate_id,
                    "web_auxiliary_source_capture_id": "current-after-search",
                    "web_auxiliary_before_action_capture_id": "current-after-search",
                    "web_auxiliary_candidate_source_capture_id": "ready",
                    "web_auxiliary_origin": ORIGIN,
                    "web_auxiliary_kind": "activity_href",
                    "destination_url": "https://example.test/campaign",
                },
            )
        )

    assert exc_info.value.error_code == "WEB_AUXILIARY_CANDIDATE_SOURCE_MISMATCH"


@pytest.mark.asyncio
async def test_semantic_resolver_rejects_origin_spoofed_snapshot() -> None:
    trusted = _snapshot(
        _element(selector="#search", text="搜索", href="https://example.test/search")
    )
    spoofed = _snapshot(
        _element(selector="#search", text="搜索", href="https://example.test/search"),
        url="https://attacker.test/source",
    )
    candidate_id = discover_web_auxiliary_navigation_candidates(trusted, origin=ORIGIN)[
        0
    ].candidate_id
    resolver = UiSnapshotSemanticTargetResolver(snapshot_reader=_SnapshotReader({"ready": spoofed}))

    with pytest.raises(SemanticTargetResolutionError):
        await resolver.resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="web:run-1",
                source_capture_id="ready",
                action_type=InteractionOperation.NAVIGATE,
                semantic_target="进入搜索页面",
                parameters={
                    "target_mode": "web_search_discovery_candidate",
                    "web_auxiliary_candidate_id": candidate_id,
                    "web_auxiliary_source_capture_id": "ready",
                    "web_auxiliary_before_action_capture_id": "ready",
                    "web_auxiliary_origin": ORIGIN,
                    "web_auxiliary_kind": "search_href",
                    "destination_url": "https://example.test/search",
                },
            )
        )


@pytest.mark.asyncio
async def test_semantic_resolver_rejects_selector_and_stale_candidate() -> None:
    snapshot = _snapshot(_element(selector="#search", text="搜索", href="/search"))
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=_SnapshotReader({"ready": snapshot})
    )
    base_parameters = {
        "target_mode": "web_search_discovery_candidate",
        "web_auxiliary_candidate_id": "stale",
        "web_auxiliary_source_capture_id": "ready",
        "web_auxiliary_before_action_capture_id": "ready",
        "web_auxiliary_origin": ORIGIN,
        "web_auxiliary_kind": "search_href",
        "destination_url": "https://example.test/search",
    }

    with pytest.raises(SemanticTargetResolutionError) as exc_info:
        await resolver.resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="web:run-1",
                source_capture_id="ready",
                action_type=InteractionOperation.NAVIGATE,
                semantic_target="进入搜索页面",
                parameters={**base_parameters, "selector": "#search"},
            )
        )

    assert exc_info.value.error_code == "WEB_AUXILIARY_CANDIDATE_TARGET_FORBIDDEN"
