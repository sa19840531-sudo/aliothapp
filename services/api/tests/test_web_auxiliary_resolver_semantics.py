"""Web 辅助导航解析器的语义目标与可信候选绑定。"""

import pytest

from services.api.app.application.interaction_runtime import (
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
)
from services.api.app.application.web_auxiliary_navigation import (
    discover_web_auxiliary_navigation_candidates,
)
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)

ORIGIN = "https://example.test/home"
DISCOVERY_CAPTURE = "capture-discovery"
BEFORE_CAPTURE = "capture-before-action"
SESSION = "web:run-1"


class SnapshotReader:
    def __init__(self, discovery, before, binding):
        self.snapshots = {DISCOVERY_CAPTURE: discovery, BEFORE_CAPTURE: before}
        self.binding = binding

    async def read_ui_snapshot(self, *, task_id, page_capture_id):
        assert task_id == "task-1"
        return self.snapshots.get(page_capture_id)

    async def read_web_before_action_binding(self, *, task_id, page_capture_id):
        assert task_id == "task-1"
        return self.binding if page_capture_id == BEFORE_CAPTURE else None


def _case(*, label, href, semantic_target, bound_target=None, declared_kind=None):
    destination = f"https://example.test{href}"
    discovery = {
        "schema_version": "ui-snapshot.v2",
        "capture_id": DISCOVERY_CAPTURE,
        "url": ORIGIN,
        "elements": [
            {
                "selector": "#auxiliary",
                "tag": "a",
                "id": label,
                "text": "",
                "href": href,
                "visible": True,
                "interactive": True,
                "disabled": False,
            }
        ],
    }
    before = {
        "schema_version": "ui-snapshot.v2",
        "capture_id": BEFORE_CAPTURE,
        "url": ORIGIN,
        "elements": [],
    }
    candidate, = discover_web_auxiliary_navigation_candidates(discovery, origin=ORIGIN)
    kind = declared_kind or candidate.kind
    parameters = {
        "target_mode": "web_search_discovery_candidate",
        "web_auxiliary_candidate_id": candidate.candidate_id,
        "web_auxiliary_source_capture_id": DISCOVERY_CAPTURE,
        "web_auxiliary_candidate_source_capture_id": DISCOVERY_CAPTURE,
        "web_auxiliary_before_action_capture_id": BEFORE_CAPTURE,
        "web_auxiliary_origin": ORIGIN,
        "web_auxiliary_kind": kind,
        "destination_url": destination,
    }
    binding = {
        "capture_id": BEFORE_CAPTURE,
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "surface_kind": "web",
        "session_id": SESSION,
        "parent_capture_id": DISCOVERY_CAPTURE,
        "action_execution_id": "action-1",
        "capture_role": "before_action",
        "capture_status": "captured",
        "session_command_sequence": 7,
        "final_url": ORIGIN,
        "parent_agent_run_id": "run-1",
        "parent_surface_kind": "web",
        "parent_session_id": SESSION,
        "parent_capture_status": "captured",
        "parent_final_url": ORIGIN,
        "action_task_id": "task-1",
        "action_session_id": SESSION,
        "action_requested_source_capture_id": DISCOVERY_CAPTURE,
        "action_source_capture_id": BEFORE_CAPTURE,
        "action_command_sequence": 7,
        "action_finalization_epoch": 3,
        "action_status": "executing",
        "action_requested_url": destination,
        "action_intent": {
            "task_id": "task-1",
            "session_id": SESSION,
            "source_capture_id": DISCOVERY_CAPTURE,
            "action_type": "navigate",
            "semantic_target": bound_target or semantic_target,
            "parameters": parameters,
        },
        "surface_current_capture_id": BEFORE_CAPTURE,
        "surface_command_sequence": 7,
        "surface_finalization_epoch": 3,
        "surface_status": "active",
    }
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=SnapshotReader(discovery, before, binding)
    )
    request = SemanticTargetResolutionRequest(
        task_id="task-1",
        session_id=SESSION,
        source_capture_id=BEFORE_CAPTURE,
        action_type=InteractionOperation.NAVIGATE,
        semantic_target=semantic_target,
        parameters=parameters,
    )
    return resolver, request, candidate


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "href", "semantic_target"),
    [
        (".search", "/search", "进入搜索页面"),
        ("css=.search", "/search", "进入搜索页面"),
        ("css=.campaign", "/campaign", "进入活动页面"),
    ],
)
async def test_resolver_accepts_fixed_semantics_for_locator_like_candidate_label(
    label, href, semantic_target
):
    resolver, request, candidate = _case(
        label=label, href=href, semantic_target=semantic_target
    )

    target = await resolver.resolve(request)

    assert target.destination_url == f"https://example.test{href}"
    assert target.target_signature == candidate.candidate_id
    assert target.target_trust == "trusted"


@pytest.mark.asyncio
async def test_resolver_rejects_wrong_semantics_even_when_action_intent_matches():
    resolver, request, _ = _case(
        label=".search", href="/search", semantic_target="进入活动页面"
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await resolver.resolve(request)

    assert error.value.error_code == "WEB_AUXILIARY_CANDIDATE_TARGET_MISMATCH"


@pytest.mark.asyncio
async def test_resolver_rejects_semantic_target_drift_from_bound_action_intent():
    resolver, request, _ = _case(
        label=".search",
        href="/search",
        semantic_target="进入搜索页面",
        bound_target="进入活动页面",
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await resolver.resolve(request)

    assert error.value.error_code == "WEB_AUXILIARY_CANDIDATE_SOURCE_MISMATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["external_promotion", "unknown_kind"])
async def test_resolver_rejects_non_navigable_kind(kind):
    resolver, request, _ = _case(
        label=".search",
        href="/search",
        semantic_target="进入搜索页面",
        declared_kind=kind,
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await resolver.resolve(request)

    assert error.value.error_code == "WEB_AUXILIARY_CANDIDATE_KIND_NOT_NAVIGABLE"


@pytest.mark.asyncio
async def test_resolver_keeps_declared_kind_bound_to_discovered_candidate():
    resolver, request, _ = _case(
        label=".search",
        href="/search",
        semantic_target="进入活动页面",
        declared_kind="activity_href",
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await resolver.resolve(request)

    assert error.value.error_code == "WEB_AUXILIARY_CANDIDATE_STALE"
