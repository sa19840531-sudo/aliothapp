"""目录外导航只能消费密封来源与当前动作前候选。"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.interaction_runtime import (
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
)
from services.api.app.application.web_runtime_discovery import discovery_entry_id
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)
from services.api.tests.test_web_desktop_navigation import (
    _runtime_navigation_candidates as extract_navigation_candidates,
)
from services.api.tests.test_web_semantic_target_resolver import SnapshotReader


def _candidate(*, href=True, source="sealed"):
    html = (
        '<nav><a href="/news">资讯</a><a href="/other">其他分类</a></nav>'
        if href
        else (
            '<nav><button type="button" onclick="openNews()">资讯</button>'
            '<a href="/other">其他分类</a></nav>'
        )
    )
    return extract_navigation_candidates(
        html, base_url="https://example.test/", source_capture_id=source, source_epoch=1
    )[0]


def test_href_less_candidate_identity_survives_source_capture_refresh():
    first = _candidate(href=False)
    second = replace(first, source_capture_id="another-state", discovery_capture_id="another-state")
    assert discovery_entry_id(first) == discovery_entry_id(second)
    assert discovery_entry_id(first) == discovery_entry_id(replace(first))


@pytest.mark.asyncio
@pytest.mark.parametrize("href", [False, True])
@pytest.mark.parametrize("drift", [False, True])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("lineage_drift", [False, True])
async def test_discovery_resolver_rechecks_sealed_candidate_for_frozen_direct_url_only(
    href, drift, nested, lineage_drift
):
    candidate = replace(
        _candidate(href=href, source="candidate-bearing"),
        discovery_capture_id="baseline",
        menu_depth=25,
    )
    if nested:
        candidate = replace(candidate, menu_path=("内容中心", "资讯"))
    element = {
        "selector": "nav a" if href else "nav button",
        "tag": "a" if href else "button",
        "type": "button",
        "role": "link" if href else "button",
        "text": "资讯",
        "href": candidate.url,
        "interactive": True,
        "visible": True,
        "disabled": False,
        "within_navigation": True,
        "is_advertisement": False,
        "rect": {"x": 0, "y": 0, "width": 100, "height": 30},
        "hit_test": {"center_hits_target": True},
    }
    sealed = {
        "capture_id": "candidate-bearing",
        "url": "https://example.test/",
        "elements": [element],
        "web_navigation": {
            "candidates": [
                replace(
                    candidate,
                    dom_fingerprint="web-nav-dom:responsive-duplicate",
                    menu_position=99,
                ).to_dict(),
                candidate.to_dict(),
            ]
        },
    }
    current = {
        "capture_id": "before",
        "url": "https://example.test/previous-menu",
        "elements": [element],
        "web_navigation": {
            "candidates": [] if drift else [replace(candidate, source_epoch=2).to_dict()]
        },
    }
    resolver = UiSnapshotSemanticTargetResolver(
        web_discovery_authorizer=SimpleNamespace(authorize=AsyncMock(return_value=True)),
        snapshot_reader=SnapshotReader(
            {"candidate-bearing": sealed, "before": current},
            expected_capture_id="candidate-bearing",
        ),
    )
    request = SemanticTargetResolutionRequest(
        task_id="task-1",
        session_id="web:run-1",
        source_capture_id="before",
        action_type=(InteractionOperation.NAVIGATE if href else InteractionOperation.CLICK),
        semantic_target=" / ".join(candidate.menu_path),
        parameters={
            "target_mode": "web_discovery_candidate",
            "web_discovery_source_capture_id": "requested",
            "web_discovery_candidate": (
                replace(
                    candidate, menu_depth=26, exploration_path=("伪造父菜单", "资讯"),
                    parent_menu_entry_id="forged-parent",
                    parent_logical_target_identity="forged-logical",
                ).to_dict()
                if lineage_drift else candidate.to_dict()
            ),
            "web_discovery_origin": "https://example.test/",
            **({"destination_url": candidate.url} if href else {}),
        },
    )
    if not href or lineage_drift:
        with pytest.raises(SemanticTargetResolutionError, match="候选"):
            await resolver.resolve(request)
    else:
        target = await resolver.resolve(request)
        assert target.target_source == "web_discovery_candidate"
        assert target.selector is None
        assert target.destination_url == "https://example.test/news"


@pytest.mark.asyncio
async def test_discovery_resolver_accepts_frontier_normalized_trailing_slash_candidate() -> None:
    original = extract_navigation_candidates(
        '<nav><a href="/news/">资讯</a><a href="/other/">其他分类</a></nav>',
        base_url="https://example.test/",
        source_epoch=3,
    )[0]
    declared = replace(
        original,
        url="https://example.test/news",
        source_capture_id="candidate-bearing",
        discovery_capture_id="candidate-bearing",
        frontier_route_shape="/news",
        frontier_dom_role="menu",
        frontier_structure_fingerprint="structure-fingerprint",
        frontier_structure_cluster_key="web-frontier-structure.v1:cluster",
        frontier_site_origin="https://example.test",
    )
    snapshots = {
        "candidate-bearing": {
            "capture_id": "candidate-bearing",
            "url": "https://example.test/",
            "web_navigation": {"candidates": [original.to_dict()]},
        },
        "before": {
            "capture_id": "before",
            "url": "https://example.test/",
            "web_navigation": {"candidates": []},
        },
    }
    resolver = UiSnapshotSemanticTargetResolver(
        web_discovery_authorizer=SimpleNamespace(authorize=AsyncMock(return_value=True)),
        snapshot_reader=SnapshotReader(
            snapshots,
            expected_capture_id="candidate-bearing",
        ),
    )
    request = SemanticTargetResolutionRequest(
        task_id="task-1",
        session_id="web:run-1",
        source_capture_id="before",
        action_type=InteractionOperation.NAVIGATE,
        semantic_target=" / ".join(declared.menu_path),
        parameters={
            "target_mode": "web_discovery_candidate",
            "web_discovery_source_capture_id": "candidate-bearing",
            "web_discovery_candidate": declared.to_dict(),
            "web_discovery_origin": "https://example.test/",
            "destination_url": "https://example.test/news",
        },
    )

    target = await resolver.resolve(request)

    assert target.destination_url == "https://example.test/news"
    assert target.target_trust == "trusted"


@pytest.mark.asyncio
async def test_discovery_resolver_rejects_cross_origin_current_capture():
    candidate = _candidate(href=True, source="candidate-bearing")
    sealed = {
        "capture_id": "candidate-bearing",
        "url": "https://example.test/",
        "web_navigation": {"candidates": [candidate.to_dict()]},
    }
    current = {
        "capture_id": "before",
        "url": "https://outside.test/redirected",
        "web_navigation": {"candidates": []},
    }
    resolver = UiSnapshotSemanticTargetResolver(
        web_discovery_authorizer=SimpleNamespace(authorize=AsyncMock(return_value=True)),
        snapshot_reader=SnapshotReader(
            {"candidate-bearing": sealed, "before": current},
            expected_capture_id="candidate-bearing",
        ),
    )
    request = SemanticTargetResolutionRequest(
        task_id="task-1",
        session_id="web:run-1",
        source_capture_id="before",
        action_type=InteractionOperation.NAVIGATE,
        semantic_target=" / ".join(candidate.menu_path),
        parameters={
            "target_mode": "web_discovery_candidate",
            "web_discovery_source_capture_id": "requested",
            "web_discovery_candidate": candidate.to_dict(),
            "web_discovery_origin": "https://example.test/",
            "destination_url": candidate.url,
        },
    )

    with pytest.raises(SemanticTargetResolutionError, match="候选"):
        await resolver.resolve(request)


@pytest.mark.asyncio
async def test_discovery_resolver_does_not_reuse_root_http_upgrade_exception() -> None:
    candidate = _candidate(href=True, source="candidate-bearing")
    snapshots = {
        "candidate-bearing": {
            "capture_id": "candidate-bearing",
            "url": "https://example.test/",
            "web_navigation": {"candidates": [candidate.to_dict()]},
        },
        "before": {
            "capture_id": "before",
            "url": "https://example.test/previous-menu",
            "web_navigation": {"candidates": []},
        },
    }
    resolver = UiSnapshotSemanticTargetResolver(
        web_discovery_authorizer=SimpleNamespace(authorize=AsyncMock(return_value=True)),
        snapshot_reader=SnapshotReader(
            snapshots,
            expected_capture_id="candidate-bearing",
        ),
    )
    request = SemanticTargetResolutionRequest(
        task_id="task-1",
        session_id="web:run-1",
        source_capture_id="before",
        action_type=InteractionOperation.NAVIGATE,
        semantic_target=" / ".join(candidate.menu_path),
        parameters={
            "target_mode": "web_discovery_candidate",
            "web_discovery_source_capture_id": "requested",
            "web_discovery_candidate": candidate.to_dict(),
            "web_discovery_origin": "http://example.test/",
            "destination_url": candidate.url,
        },
    )

    with pytest.raises(SemanticTargetResolutionError, match="候选"):
        await resolver.resolve(request)


def test_explicit_web_discovery_retains_visible_rejected_candidates_for_audit():
    candidates = extract_navigation_candidates(
        '<nav><a href="/login">登录</a><a href="https://outside.test/">外部</a>'
        '<a download href="/file">获取文件</a><a href="/news">资讯</a>'
        '<a href="/other">其他分类</a></nav>',
        base_url="https://example.test/",
        include_blocked=True,
    )
    assert len(candidates) == 5
    assert sum(item.actionability == "blocked" for item in candidates) == 3
    from services.api.app.application.web_runtime_discovery import discovery_safety_reason

    outside = next(item for item in candidates if item.label == "外部")
    assert (
        discovery_safety_reason(outside, site_origin="https://example.test/")
        == "WEB_DISCOVERY_CROSS_ORIGIN_BLOCKED"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("href,same_url", [(True, False)])
async def test_discovery_visitor_uses_explicit_runtime_direct_url_contract(
    href, same_url
):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from services.api.app.application.web_desktop_exploration import WebDesktopMenuPageVisitor
    from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
    from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry
    from services.api.app.domain.exploration import WebMenuTerminalStatus

    candidate = replace(_candidate(href=href), menu_depth=25)
    actions = []

    class Coordinator:
        async def coordinate(self, command):
            actions.append(command.intent)
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id="arrived"),
                interaction=SimpleNamespace(
                    execution_outcome="succeeded",
                    status="succeeded",
                    action_execution_id="discovery-action",
                ),
            )

    class Reader:
        async def read_url(self, *, capture_id):
            return (
                "https://example.test/"
                if capture_id == "sealed" or same_url
                else "https://example.test/news"
            )

        async def read_snapshot(self, *, capture_id):
            return {"elements": [{"tag": "main", "visible": True, "text": capture_id}]}

    result = await WebDesktopMenuPageVisitor(
        coordinator=Coordinator(), capture_url_reader=Reader()
    ).visit(
        command=RunWebWorkflowCommand(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            session_owner="web-v2-workflow",
            target_url="https://example.test/",
            browser_profile_id="desktop_fhd_1920x1080_v1",
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        ),
        entry=WebRuntimeMenuEntry.from_candidate(candidate),
        candidate=candidate,
        source_capture_id="sealed",
        menu_intent_id="discovery-reserved",
    )
    assert actions[0].parameters["target_mode"] == "web_discovery_candidate"
    assert actions[0].parameters["navigation_depth"] == 25
    assert actions[0].action_type is InteractionOperation.NAVIGATE
    frozen_candidate = dict(actions[0].parameters["web_discovery_candidate"])
    frozen_candidate["menu_path"] = list(frozen_candidate["menu_path"])
    frozen_candidate["navigation_evidence"] = list(frozen_candidate["navigation_evidence"])
    frozen_candidate["exploration_path"] = list(frozen_candidate["exploration_path"])
    if frozen_candidate["navigation_container_restore_seed"] is not None:
        seed = dict(frozen_candidate["navigation_container_restore_seed"])
        if "observations" in seed:
            seed["observations"] = [
                [*observation[:3], list(observation[3])]
                for observation in seed["observations"]
            ]
        frozen_candidate["navigation_container_restore_seed"] = seed
    assert frozen_candidate == candidate.to_dict()
    assert "web_navigation_expected_url" not in actions[0].parameters
    assert ("destination_url" in actions[0].parameters) is href
    assert result.status is WebMenuTerminalStatus.MENU_PAGE_CAPTURED
    assert result.menu_observed_target_url == "https://example.test/news"


def test_href_less_discovery_candidate_is_audit_only() -> None:
    from services.api.app.application.web_runtime_discovery import discovery_safety_reason

    assert discovery_safety_reason(
        _candidate(href=False), site_origin="https://example.test/"
    ) == "WEB_DISCOVERY_DIRECT_URL_REQUIRED"


def test_hidden_href_discovery_candidate_is_direct_navigation_eligible() -> None:
    from services.api.app.application.web_runtime_discovery import discovery_safety_reason

    assert (
        discovery_safety_reason(
            replace(_candidate(href=True), visibility="hidden"),
            site_origin="https://example.test/",
        )
        is None
    )


@pytest.mark.asyncio
async def test_discovered_href_menu_uses_direct_navigation() -> None:
    from datetime import UTC, datetime, timedelta

    from services.api.app.application.web_desktop_exploration import WebDesktopMenuPageVisitor
    from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
    from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry

    candidate = replace(
        _candidate(href=True),
        url="https://example.test/news?utm_source=navigation-test",
    )
    actions = []

    class Coordinator:
        async def coordinate(self, command):
            actions.append(command.intent)
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id="arrived"),
                interaction=SimpleNamespace(
                    execution_outcome="succeeded",
                    status="succeeded",
                    action_execution_id="direct-navigation",
                ),
            )

    class Reader:
        async def read_url(self, *, capture_id):
            return (
                "https://example.test/"
                if capture_id == "sealed"
                else "https://example.test/news"
            )

        async def read_snapshot(self, *, capture_id):
            return {"elements": [{"tag": "main", "visible": True, "text": capture_id}]}

    await WebDesktopMenuPageVisitor(
        coordinator=Coordinator(), capture_url_reader=Reader()
    ).visit(
        command=RunWebWorkflowCommand(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            session_owner="web-v2-workflow",
            target_url="https://example.test/",
            browser_profile_id="desktop_fhd_1920x1080_v1",
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        ),
        entry=WebRuntimeMenuEntry.from_candidate(candidate),
        candidate=candidate,
        source_capture_id="sealed",
        menu_intent_id="discovery-direct-navigation",
    )

    assert len(actions) == 1
    assert actions[0].action_type is InteractionOperation.NAVIGATE
    assert actions[0].parameters["destination_url"] == "https://example.test/news"


@pytest.mark.asyncio
async def test_discovered_href_less_menu_never_coordinates_click() -> None:
    from datetime import UTC, datetime, timedelta

    from services.api.app.application.web_desktop_exploration import WebDesktopMenuPageVisitor
    from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
    from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry

    candidate = _candidate(href=False)
    actions = []

    class Coordinator:
        async def coordinate(self, command):
            actions.append(command.intent)
            raise AssertionError("无 href 菜单不得进入动作协调器")

    class Reader:
        async def read_url(self, *, capture_id):
            del capture_id
            return "https://example.test/"

    with pytest.raises(ValueError, match="WEB_MENU_DIRECT_URL_REQUIRED"):
        await WebDesktopMenuPageVisitor(
            coordinator=Coordinator(), capture_url_reader=Reader()
        ).visit(
            command=RunWebWorkflowCommand(
                task_id="task-1",
                agent_run_id="run-1",
                session_id="web:run-1",
                session_owner="web-v2-workflow",
                target_url="https://example.test/",
                browser_profile_id="desktop_fhd_1920x1080_v1",
                deadline=datetime.now(UTC) + timedelta(minutes=5),
            ),
            entry=WebRuntimeMenuEntry.from_candidate(candidate),
            candidate=candidate,
            source_capture_id="sealed",
            menu_intent_id="discovery-no-href",
        )

    assert actions == []


@pytest.mark.parametrize(
    "control",
    [
        '<form><button type="submit">资讯</button></form>',
        '<form><button type="button">资讯</button></form>',
        '<button form="account">资讯</button>',
        '<a download href="/news">资讯</a>',
    ],
)
def test_discovery_form_or_download_controls_are_denied_despite_low_self_report(control):
    from services.api.app.application.web_runtime_discovery import discovery_safety_reason

    candidate = extract_navigation_candidates(
        "<nav>" + control + "</nav>", base_url="https://example.test/", include_blocked=True
    )[0]
    assert (
        discovery_safety_reason(
            replace(candidate, risk_level="low", actionability="requires_runtime_validation"),
            site_origin="https://example.test/",
        )
        is not None
    )


def test_enabled_browser_refreshes_no_href_candidate_without_repeating_discovery():
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureArtifact,
        PageCaptureMode,
        PageCaptureRequest,
        PageCaptureResult,
    )
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    browser = WebDesktopCapturePolicyBrowser(browser=object(), include_blocked_navigation=True)
    request = PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE,
        session_id="web:run-1",
        viewport_width=1920,
        viewport_height=1080,
        web_capture_stage="web_page_probe",
    )
    for capture_id, url in [
        ("page-one", "https://example.test/one"),
        ("page-two", "https://example.test/two"),
    ]:
        browser._apply_result_policy(
            request,
            PageCaptureResult(
                execution_status="success",
                mode="observe",
                reachable=True,
                final_url=url,
                title="test",
                status_code=200,
                artifacts=[
                    PageCaptureArtifact(
                        "dom",
                        None,
                        '<nav><button type="button">资讯</button><a href="/login">登录</a></nav>',
                    )
                ],
            ),
        )
        browser.bind_latest_navigation_capture("web:run-1", capture_id)
    candidates = browser.navigation_candidates("web:run-1")
    controls = [item for item in candidates if item.label == "资讯"]
    assert len(controls) == 1
    assert controls[0].discovery_capture_id == "page-one"
    assert controls[0].source_capture_id == "page-two"
    assert any(item.label == "登录" and item.actionability == "blocked" for item in candidates)


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", ["web:disabled-run", "h5:run-1"])
async def test_shared_resolver_denies_forged_discovery_mode_without_injected_authority(session_id):
    candidate = _candidate()
    resolver = UiSnapshotSemanticTargetResolver(snapshot_reader=SnapshotReader({}))
    with pytest.raises(SemanticTargetResolutionError, match="授权"):
        await resolver.resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id=session_id,
                source_capture_id="before",
                action_type=InteractionOperation.NAVIGATE,
                semantic_target="资讯",
                parameters={
                    "target_mode": "web_discovery_candidate",
                    "web_discovery_candidate": candidate.to_dict(),
                    "web_discovery_source_capture_id": "sealed",
                    "web_discovery_origin": "https://example.test/",
                    "destination_url": candidate.url,
                    "enabled": True,
                },
            )
        )


@pytest.mark.asyncio
async def test_no_href_ready_returning_to_source_never_reuses_old_classification():
    from services.api.app.application.web_menu_exploration import WebMenuVisitResult
    from services.api.app.application.web_runtime_composition import WebOrchestratedMenuPageVisitor
    from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry
    from services.api.app.domain.exploration import WebMenuTerminalStatus

    classifier = SimpleNamespace(
        classify=AsyncMock(
            return_value=SimpleNamespace(
                page_type="content_detail",
                reused_capture_id="old-home",
                reused_logical_page_id="old-home-logical",
            )
        )
    )
    probe = WebMenuVisitResult(
        final_capture_id="probe",
        page_type=None,
        status=WebMenuTerminalStatus.MENU_PAGE_CAPTURED,
        menu_before_url="https://example.test/",
        menu_observed_target_url="https://example.test/news",
    )
    visitor = WebOrchestratedMenuPageVisitor(
        navigator=SimpleNamespace(visit=AsyncMock(return_value=probe)),
        readiness=SimpleNamespace(
            prepare=AsyncMock(
                return_value=SimpleNamespace(
                    status="ready",
                    ready_capture_id="ready",
                    evidence_capture_ids=("probe", "ready"),
                    dismissed_layers=(),
                )
            )
        ),
        ready_capture_reader=SimpleNamespace(
            read=AsyncMock(
                return_value=SimpleNamespace(
                    capture_id="ready",
                    url="https://example.test/",
                    interaction_facts=SimpleNamespace(url="https://example.test/"),
                )
            )
        ),
        classifier=classifier,
        evidence_source=object(),
        detail_navigator=object(),
    )
    result = await visitor.visit(
        command=SimpleNamespace(session_id="web:run-1"),
        entry=WebRuntimeMenuEntry.from_candidate(_candidate(href=False)),
    )
    classifier.classify.assert_not_awaited()
    assert result.error_code == "WEB_DISCOVERY_SAME_URL_OBJECT_UNPROVEN"
