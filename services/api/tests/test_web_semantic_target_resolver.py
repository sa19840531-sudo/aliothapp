"""Web 密封候选到可信 selector 的运行时绑定合同。"""

import pytest

from services.api.app.application.interaction_runtime import (
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
)
from services.api.app.application.web_list_detail_navigation import (
    detail_candidates_from_ready_snapshot,
)
from services.api.app.application.web_logical_page import web_logical_page_id
from services.api.app.application.web_primary_content_capture import (
    build_web_primary_content_targets,
)
from services.api.app.domain.exploration import build_web_obstruction_candidates
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)


class SnapshotReader:
    def __init__(self, snapshot, *, expected_capture_id="capture-entry"):
        self.snapshots = (
            snapshot
            if isinstance(snapshot, dict) and expected_capture_id in snapshot
            else {expected_capture_id: snapshot}
        )

    async def read_ui_snapshot(self, *, task_id, page_capture_id):
        assert task_id == "task-1"
        return self.snapshots[page_capture_id]


class DetailBindingReader(SnapshotReader):
    def __init__(self, snapshots, *, binding):
        super().__init__(snapshots)
        self.binding = binding

    async def read_web_before_action_binding(self, *, task_id, page_capture_id):
        assert task_id == "task-1"
        return self.binding if page_capture_id == "capture-before" else None


def _detail_url_snapshot(*, capture_id: str, url: str) -> dict[str, object]:
    return {
        "capture_id": capture_id,
        "url": url,
        "structure_signature": f"signature-{capture_id}",
        "elements": [
            {
                "selector": "#card-1 a",
                "tag": "a",
                "role": "link",
                "text": "内容标题",
                "href": "article/1",
                "rect": {"x": 100, "y": 1200, "width": 300, "height": 180},
                "interactive": True,
                "visible": True,
                "within_repeated_structure": True,
                "ancestor_class_tokens": ["article-card", "content-grid"],
            }
        ],
    }


def _detail_url_parameters(candidate_id: str) -> dict[str, object]:
    return {
        "target_mode": "web_detail_candidate_url",
        "destination_url": "https://example.test/list/article/1",
        "web_detail_candidate_id": candidate_id,
        "web_detail_source_capture_id": "capture-entry",
        "web_detail_before_action_capture_id": "capture-before",
        "web_detail_list_page_type": "content_list",
        "web_detail_origin": "https://example.test",
        "web_detail_current_url": "https://example.test/list/",
    }


def _detail_before_action_binding(
    *,
    candidate_id: str,
    changes: dict[str, object] | None = None,
) -> dict[str, object]:
    parameters = _detail_url_parameters(candidate_id)
    parameters.pop("web_detail_before_action_capture_id")
    binding: dict[str, object] = {
        "capture_id": "capture-before",
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "surface_kind": "web",
        "session_id": "web:run-1",
        "parent_capture_id": "capture-entry",
        "action_execution_id": "action-detail",
        "capture_role": "before_action",
        "capture_status": "captured",
        "session_command_sequence": 7,
        "final_url": "https://example.test/list/",
        "capture_requested_url": None,
        "parent_agent_run_id": "run-1",
        "parent_surface_kind": "web",
        "parent_session_id": "web:run-1",
        "parent_capture_status": "captured",
        "parent_final_url": "https://example.test/list/",
        "action_task_id": "task-1",
        "action_session_id": "web:run-1",
        "action_requested_source_capture_id": "capture-entry",
        "action_source_capture_id": "capture-before",
        "action_command_sequence": 7,
        "action_finalization_epoch": 3,
        "action_status": "executing",
        "action_requested_url": "https://example.test/list/article/1",
        "action_intent": {
            "task_id": "task-1",
            "session_id": "web:run-1",
            "source_capture_id": "capture-entry",
            "action_type": "navigate",
            "semantic_target": "打开当前列表可信详情",
            "parameters": parameters,
        },
        "surface_current_capture_id": "capture-before",
        "surface_command_sequence": 7,
        "surface_finalization_epoch": 3,
        "surface_status": "active",
    }
    binding.update(changes or {})
    return binding


@pytest.mark.asyncio
async def test_resolves_allowlisted_href_less_navigation_candidate_for_click() -> None:
    snapshot = {
        "schema_version": "ui-snapshot.v2",
        "capture_id": "capture-entry",
        "structure_signature": "signature-navigation",
        "web_navigation": {
            "schema_version": "web-navigation.v1",
            "candidates": [
                {
                    "entry_id": "web-nav-entry:unbound",
                    "label": "AI短剧",
                    "url": None,
                    "menu_path": ["AI短剧"],
                    "dom_fingerprint": "web-nav-dom:button-1",
                    "source_kind": "button",
                    "target_hint": "AI短剧",
                    "source_epoch": 3,
                    "visibility": "visible",
                    "actionability": "requires_runtime_validation",
                    "risk_level": "low",
                }
            ],
        },
        "elements": [
            {
                "selector": "nav button:nth-of-type(3)",
                "tag": "button",
                "role": "button",
                "text": "AI短剧",
                "rect": {"x": 400, "y": 80, "width": 120, "height": 42},
                "interactive": True,
                "visible": True,
                "disabled": False,
                "within_navigation": True,
                "is_advertisement": False,
                "hit_test": {"center_hits_target": True},
            }
        ],
    }

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=SnapshotReader(snapshot)
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-1",
            source_capture_id="capture-entry",
            action_type=InteractionOperation.CLICK,
            semantic_target="AI短剧",
            parameters={
                "target_mode": "web_navigation_candidate",
                "web_navigation_candidate_id": "web-nav-entry:bound",
                "web_navigation_source_capture_id": "capture-entry",
                "web_navigation_dom_fingerprint": "web-nav-dom:button-1",
                "web_navigation_menu_path": ["AI短剧"],
                "web_navigation_target_hint": "AI短剧",
                "web_navigation_source_kind": "button",
                "web_navigation_source_epoch": 3,
                "web_navigation_expected_url": "https://example.test/ai-drama",
            },
        )
    )

    assert target.selector == "nav button:nth-of-type(3)"
    assert target.target_source == "web_navigation_candidate"
    assert target.target_signature == "web-nav-entry:bound"


def _snapshot(*, text: str = "我已年满18岁并进入") -> dict[str, object]:
    return {
        "schema_version": "ui-snapshot.v2",
        "structure_signature": "signature-1",
        "elements": [
            {
                "selector": "#adult-confirm",
                "tag": "button",
                "role": "button",
                "text": text,
                "class_tokens": ["confirm"],
                "ancestor_class_tokens": ["age-gate"],
                "web_obstruction_layer": "#entry-age-gate",
                "web_obstruction_layer_kind": "age_gate",
                "web_obstruction_layer_structural": True,
                "web_obstruction_close_semantics": True,
                "web_obstruction_close_basis": "age_gate_confirmation",
                "web_obstruction_close_control_unique": True,
                "web_obstruction_close_hit_safe": True,
                "web_human_verification_present": False,
                "within_web_navigation": False,
                "rect": {"x": 760, "y": 600, "width": 400, "height": 60},
                "interactive": True,
                "visible": True,
                "effective_z_index": 900,
                "hit_test": {"center_hits_target": True},
            }
        ],
    }


def _close_image_snapshot(*, alt: str | None = "关闭") -> dict[str, object]:
    return {
        "schema_version": "ui-snapshot.v2",
        "structure_signature": "signature-close-image",
        "elements": [
            {
                "selector": "#popup img.close-image",
                "tag": "img",
                "role": None,
                "text": "",
                "aria_label": None,
                "alt": alt,
                "class_tokens": ["close-image", "on-dialog-close"],
                "ancestor_class_tokens": ["close-img", "van-popup"],
                "web_obstruction_layer": "#popup",
                "web_obstruction_layer_kind": "dialog",
                "web_obstruction_layer_structural": True,
                "web_obstruction_close_semantics": True,
                "web_obstruction_close_basis": "delegated_wrapper_self",
                "web_obstruction_close_control_unique": True,
                "web_obstruction_close_hit_safe": True,
                "web_human_verification_present": False,
                "within_web_navigation": False,
                "rect": {"x": 942, "y": 750, "width": 35, "height": 35},
                "interactive": True,
                "visible": True,
                "effective_z_index": 100,
                "hit_test": {"center_hits_target": True},
            }
        ],
    }


def _primary_content_snapshot(*, url: str) -> dict[str, object]:
    return {
        "schema_version": "ui-snapshot.v2",
        "url": url,
        "structure_signature": "signature-primary-content",
        "elements": [
            {
                "selector": "#article-body",
                "tag": "article",
                "class_tokens": ["article-body"],
                "dom_fingerprint": "dom-primary-content",
                "visible": True,
            }
        ],
    }


@pytest.mark.asyncio
async def test_primary_content_scroll_rebinds_only_to_before_action_capture() -> None:
    page_url = "https://example.test/articles/42?utm_source=campaign"
    discovery_snapshot = _primary_content_snapshot(url=page_url)
    before_snapshot = _primary_content_snapshot(url=page_url)
    candidate = build_web_primary_content_targets(
        capture_id="capture-discovery",
        ui_snapshot=discovery_snapshot,
        expected_page_type="content_detail",
    )[0]

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=SnapshotReader(
            {
                "capture-discovery": discovery_snapshot,
                "capture-before-action": before_snapshot,
            },
            expected_capture_id="capture-before-action",
        )
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-1",
            source_capture_id="capture-before-action",
            action_type=InteractionOperation.SCROLL,
            semantic_target="滚动到非广告主体内容",
            parameters={
                "target_mode": "web_primary_content_candidate",
                "web_primary_content_candidate_id": candidate.candidate_id,
                "web_primary_content_discovery_capture_id": "capture-discovery",
                "web_primary_content_source_capture_id": "capture-discovery",
                "web_primary_content_before_action_capture_id": "capture-before-action",
                "expected_page_type": "content_detail",
                "logical_page_id": web_logical_page_id(page_url),
            },
        )
    )

    assert target.selector == "#article-body"
    assert target.target_signature == candidate.candidate_id


@pytest.mark.asyncio
async def test_primary_content_scroll_rejects_changed_before_action_logical_page() -> None:
    discovery_url = "https://example.test/articles/42"
    discovery_snapshot = _primary_content_snapshot(url=discovery_url)
    candidate = build_web_primary_content_targets(
        capture_id="capture-discovery",
        ui_snapshot=discovery_snapshot,
        expected_page_type="content_detail",
    )[0]

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(
            snapshot_reader=SnapshotReader(
                {
                    "capture-discovery": discovery_snapshot,
                    "capture-before-action": _primary_content_snapshot(
                        url="https://example.test/articles/99"
                    ),
                },
                expected_capture_id="capture-before-action",
            )
        ).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="web:run-1",
                source_capture_id="capture-before-action",
                action_type=InteractionOperation.SCROLL,
                semantic_target="滚动到非广告主体内容",
                parameters={
                    "target_mode": "web_primary_content_candidate",
                    "web_primary_content_candidate_id": candidate.candidate_id,
                    "web_primary_content_discovery_capture_id": "capture-discovery",
                    "web_primary_content_source_capture_id": "capture-discovery",
                    "web_primary_content_before_action_capture_id": "capture-before-action",
                    "expected_page_type": "content_detail",
                    "logical_page_id": web_logical_page_id(discovery_url),
                },
            )
        )

    assert error.value.error_code == "WEB_PRIMARY_CONTENT_LOGICAL_PAGE_MISMATCH"


@pytest.mark.asyncio
async def test_primary_content_scroll_rejects_changed_discovery_logical_page() -> None:
    page_url = "https://example.test/articles/42"
    before_snapshot = _primary_content_snapshot(url=page_url)
    candidate = build_web_primary_content_targets(
        capture_id="capture-discovery",
        ui_snapshot=before_snapshot,
        expected_page_type="content_detail",
    )[0]

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(
            snapshot_reader=SnapshotReader(
                {
                    "capture-discovery": _primary_content_snapshot(
                        url="https://example.test/articles/99"
                    ),
                    "capture-before-action": before_snapshot,
                },
                expected_capture_id="capture-before-action",
            )
        ).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="web:run-1",
                source_capture_id="capture-before-action",
                action_type=InteractionOperation.SCROLL,
                semantic_target="滚动到非广告主体内容",
                parameters={
                    "target_mode": "web_primary_content_candidate",
                    "web_primary_content_candidate_id": candidate.candidate_id,
                    "web_primary_content_discovery_capture_id": "capture-discovery",
                    "web_primary_content_source_capture_id": "capture-discovery",
                    "web_primary_content_before_action_capture_id": "capture-before-action",
                    "expected_page_type": "content_detail",
                    "logical_page_id": web_logical_page_id(page_url),
                },
            )
        )

    assert error.value.error_code == "WEB_PRIMARY_CONTENT_DISCOVERY_LOGICAL_PAGE_MISMATCH"


@pytest.mark.asyncio
async def test_primary_content_scroll_rejects_candidate_absent_from_discovery_capture() -> None:
    page_url = "https://example.test/articles/42"
    current_snapshot = _primary_content_snapshot(url=page_url)
    candidate = build_web_primary_content_targets(
        capture_id="capture-discovery",
        ui_snapshot=current_snapshot,
        expected_page_type="content_detail",
    )[0]

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(
            snapshot_reader=SnapshotReader(
                {
                    "capture-discovery": {
                        "url": page_url,
                        "structure_signature": "signature-without-primary-content",
                        "elements": [],
                    },
                    "capture-before-action": current_snapshot,
                },
                expected_capture_id="capture-before-action",
            )
        ).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="web:run-1",
                source_capture_id="capture-before-action",
                action_type=InteractionOperation.SCROLL,
                semantic_target="滚动到非广告主体内容",
                parameters={
                    "target_mode": "web_primary_content_candidate",
                    "web_primary_content_candidate_id": candidate.candidate_id,
                    "web_primary_content_discovery_capture_id": "capture-discovery",
                    "web_primary_content_source_capture_id": "capture-discovery",
                    "web_primary_content_before_action_capture_id": "capture-before-action",
                    "expected_page_type": "content_detail",
                    "logical_page_id": web_logical_page_id(page_url),
                },
            )
        )

    assert error.value.error_code == "WEB_PRIMARY_CONTENT_DISCOVERY_CANDIDATE_MISMATCH"


@pytest.mark.asyncio
async def test_resolves_safe_web_sealed_candidate_without_selector_parameter() -> None:
    snapshot = _snapshot()
    candidate = build_web_obstruction_candidates(
        capture_id="capture-entry",
        ui_snapshot=snapshot,
    )[0]
    parameters = {
        "target_mode": "web_sealed_candidate",
        "web_sealed_candidate_id": candidate.candidate_id,
        "web_sealed_source_capture_id": "capture-entry",
        "web_sealed_candidate_capability": "web_age_gate_confirm",
    }

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=SnapshotReader(snapshot)
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-1",
            source_capture_id="capture-entry",
            action_type=InteractionOperation.CLICK,
            semantic_target="确认已成年并进入",
            parameters=parameters,
        )
    )

    assert "selector" not in parameters
    assert target.selector == "#adult-confirm"
    assert dict(target.expected_rect or {}) == candidate.rect
    assert target.target_source == "web_sealed_candidate"
    assert target.target_signature == candidate.candidate_id
    assert target.target_trust == "trusted"


@pytest.mark.asyncio
async def test_resolves_structural_close_image_from_alt_semantics() -> None:
    snapshot = _close_image_snapshot()
    candidate = build_web_obstruction_candidates(
        capture_id="capture-entry",
        ui_snapshot=snapshot,
    )[0]

    assert candidate.text == "关闭"

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=SnapshotReader(snapshot)
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-1",
            source_capture_id="capture-entry",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前阻断弹窗",
            parameters={
                "target_mode": "web_sealed_candidate",
                "web_sealed_candidate_id": candidate.candidate_id,
                "web_sealed_source_capture_id": "capture-entry",
                "web_sealed_candidate_capability": "web_obstruction_dismiss",
            },
        )
    )

    assert target.selector == "#popup img.close-image"


@pytest.mark.asyncio
async def test_rejects_structural_close_image_with_unsafe_alt_semantics() -> None:
    snapshot = _close_image_snapshot(alt="点击下载")
    candidate = build_web_obstruction_candidates(
        capture_id="capture-entry",
        ui_snapshot=snapshot,
    )[0]

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=SnapshotReader(snapshot)).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="web:run-1",
                source_capture_id="capture-entry",
                action_type=InteractionOperation.CLICK,
                semantic_target="关闭当前阻断弹窗",
                parameters={
                    "target_mode": "web_sealed_candidate",
                    "web_sealed_candidate_id": candidate.candidate_id,
                    "web_sealed_source_capture_id": "capture-entry",
                    "web_sealed_candidate_capability": "web_obstruction_dismiss",
                },
            )
        )

    assert error.value.error_code == "WEB_SEALED_CANDIDATE_RISK_BLOCKED"


@pytest.mark.asyncio
async def test_resolves_delegated_structural_close_without_accessible_text() -> None:
    snapshot = _close_image_snapshot(alt=None)
    candidate = build_web_obstruction_candidates(
        capture_id="capture-entry",
        ui_snapshot=snapshot,
    )[0]

    assert candidate.text == "关闭"

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=SnapshotReader(snapshot)
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-1",
            source_capture_id="capture-entry",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前阻断弹窗",
            parameters={
                "target_mode": "web_sealed_candidate",
                "web_sealed_candidate_id": candidate.candidate_id,
                "web_sealed_source_capture_id": "capture-entry",
                "web_sealed_candidate_capability": "web_obstruction_dismiss",
            },
        )
    )

    assert target.selector == "#popup img.close-image"


def test_suppresses_empty_close_candidate_in_human_verification_layer() -> None:
    snapshot = _close_image_snapshot(alt=None)
    snapshot["elements"].append(
        {
            "selector": "#popup .captcha-copy",
            "tag": "div",
            "text": "请完成人机验证码",
            "class_tokens": ["captcha-copy"],
            "ancestor_class_tokens": ["van-popup"],
            "web_obstruction_layer": "#popup",
            "rect": {"x": 700, "y": 400, "width": 520, "height": 80},
            "interactive": False,
            "visible": True,
            "effective_z_index": 100,
            "hit_test": {"center_hits_target": True},
        }
    )

    assert (
        build_web_obstruction_candidates(
            capture_id="capture-entry",
            ui_snapshot=snapshot,
        )
        == ()
    )


@pytest.mark.asyncio
async def test_rebinds_web_candidate_on_fresh_before_capture() -> None:
    snapshot = _snapshot()
    selected = build_web_obstruction_candidates(
        capture_id="capture-ready",
        ui_snapshot=snapshot,
    )[0]

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=SnapshotReader(
            {
                "capture-ready": snapshot,
                "capture-fresh-before": snapshot,
            },
            expected_capture_id="capture-fresh-before",
        )
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-1",
            source_capture_id="capture-fresh-before",
            action_type=InteractionOperation.CLICK,
            semantic_target="确认已成年并进入",
            parameters={
                "target_mode": "web_sealed_candidate",
                "web_sealed_candidate_id": selected.candidate_id,
                "web_sealed_source_capture_id": "capture-ready",
                "web_sealed_candidate_capability": "web_age_gate_confirm",
            },
        )
    )

    assert target.selector == "#adult-confirm"
    assert target.target_signature == selected.candidate_id


@pytest.mark.asyncio
async def test_rejects_candidate_not_present_in_declared_source_capture() -> None:
    snapshot = _snapshot()
    other_capture_candidate = build_web_obstruction_candidates(
        capture_id="capture-other",
        ui_snapshot=snapshot,
    )[0]
    declared_source_snapshot = _snapshot(text="继续浏览")

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(
            snapshot_reader=SnapshotReader(
                {
                    "capture-origin": declared_source_snapshot,
                    "capture-entry": snapshot,
                }
            )
        ).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="web:run-1",
                source_capture_id="capture-entry",
                action_type=InteractionOperation.CLICK,
                semantic_target="确认已成年并进入",
                parameters={
                    "target_mode": "web_sealed_candidate",
                    "web_sealed_candidate_id": other_capture_candidate.candidate_id,
                    "web_sealed_source_capture_id": "capture-origin",
                    "web_sealed_candidate_capability": "web_age_gate_confirm",
                },
            )
        )

    assert error.value.error_code == "WEB_SEALED_CANDIDATE_SOURCE_MISMATCH"


def test_suppresses_age_gate_candidate_with_login_text() -> None:
    snapshot = _snapshot(text="我已年满18岁，登录后继续")

    assert (
        build_web_obstruction_candidates(
            capture_id="capture-entry",
            ui_snapshot=snapshot,
        )
        == ()
    )


@pytest.mark.asyncio
async def test_resolves_detail_candidate_from_source_ready_snapshot() -> None:
    snapshot = {
        "capture_id": "capture-entry",
        "structure_signature": "signature-detail-list",
        "elements": [
            {
                "selector": "#card-1 a",
                "tag": "a",
                "role": "link",
                "text": "内容标题",
                "href": "/article/1",
                "rect": {"x": 100, "y": 200, "width": 300, "height": 180},
                "interactive": True,
                "visible": True,
                "hit_test": {"center_hits_target": True},
                "within_repeated_structure": True,
                "ancestor_class_tokens": ["article-card", "content-grid"],
            }
        ],
    }
    candidate = detail_candidates_from_ready_snapshot(
        snapshot,
        list_page_type="content_list",
        origin="https://example.test",
    )[0]
    parameters = {
        "target_mode": "web_detail_candidate",
        "web_detail_candidate_id": candidate.candidate_id,
        "web_detail_source_capture_id": "capture-entry",
        "web_detail_list_page_type": "content_list",
        "web_detail_origin": "https://example.test",
        "web_detail_current_url": "https://example.test/list",
    }

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=SnapshotReader(snapshot)
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-1",
            source_capture_id="capture-entry",
            action_type=InteractionOperation.CLICK,
            semantic_target="打开当前列表可信详情",
            parameters=parameters,
        )
    )

    assert target.selector == "#card-1 a"
    assert target.target_source == "web_detail_candidate"
    assert target.target_signature == candidate.candidate_id

    scroll_target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=SnapshotReader(snapshot)
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-1",
            source_capture_id="capture-entry",
            action_type=InteractionOperation.SCROLL,
            semantic_target="滚动到当前列表可信详情",
            parameters=parameters,
        )
    )

    assert scroll_target.selector == "#card-1 a"
    assert scroll_target.target_signature == candidate.candidate_id


@pytest.mark.asyncio
async def test_resolves_detail_candidate_url_only_when_sealed_source_matches() -> None:
    snapshot = _detail_url_snapshot(
        capture_id="capture-entry",
        url="https://example.test/list/",
    )
    candidate = detail_candidates_from_ready_snapshot(
        snapshot,
        list_page_type="content_list",
        origin="https://example.test",
        current_url="https://example.test/list/",
    )[0]
    parameters = _detail_url_parameters(candidate.candidate_id)
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=DetailBindingReader(
            {
                "capture-entry": snapshot,
                "capture-before": {
                    **snapshot,
                    "capture_id": "capture-before",
                },
            },
            binding=_detail_before_action_binding(candidate_id=candidate.candidate_id),
        )
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-1",
            source_capture_id="capture-before",
            action_type=InteractionOperation.NAVIGATE,
            semantic_target="打开当前列表可信详情",
            parameters=parameters,
        )
    )

    assert target.destination_url == candidate.canonical_url
    assert target.target_source == "web_detail_candidate_url"
    assert target.target_signature == candidate.candidate_id

    mismatched_parameters = {
        **parameters,
        "destination_url": "https://example.test/article/2",
    }
    mismatched_binding = _detail_before_action_binding(candidate_id=candidate.candidate_id)
    mismatched_binding["action_requested_url"] = mismatched_parameters["destination_url"]
    mismatched_binding["action_intent"] = {
        **mismatched_binding["action_intent"],
        "parameters": {
            name: value
            for name, value in mismatched_parameters.items()
            if name != "web_detail_before_action_capture_id"
        },
    }
    mismatched_resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=DetailBindingReader(
            {
                "capture-entry": snapshot,
                "capture-before": {
                    **snapshot,
                    "capture_id": "capture-before",
                },
            },
            binding=mismatched_binding,
        )
    )
    with pytest.raises(SemanticTargetResolutionError) as error:
        await mismatched_resolver.resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="web:run-1",
                source_capture_id="capture-before",
                action_type=InteractionOperation.NAVIGATE,
                semantic_target="打开当前列表可信详情",
                parameters=mismatched_parameters,
            )
        )

    assert error.value.error_code == "WEB_DETAIL_CANDIDATE_URL_MISMATCH"


@pytest.mark.asyncio
async def test_detail_candidate_url_rejects_request_current_url_that_differs_from_snapshot() -> (
    None
):
    snapshot = {
        "capture_id": "capture-entry",
        "url": "https://example.test/list/",
        "structure_signature": "signature-detail-list",
        "elements": [
            {
                "selector": "#card-1 a",
                "tag": "a",
                "role": "link",
                "text": "内容标题",
                "href": "article/1",
                "rect": {"x": 100, "y": 1200, "width": 300, "height": 180},
                "interactive": True,
                "visible": True,
                "within_repeated_structure": True,
                "ancestor_class_tokens": ["article-card", "content-grid"],
            }
        ],
    }
    forged = detail_candidates_from_ready_snapshot(
        snapshot,
        list_page_type="content_list",
        origin="https://example.test",
        current_url="https://example.test/admin/",
    )[0]
    parameters = {
        "target_mode": "web_detail_candidate_url",
        "destination_url": forged.canonical_url,
        "web_detail_candidate_id": forged.candidate_id,
        "web_detail_source_capture_id": "capture-entry",
        "web_detail_before_action_capture_id": "capture-before",
        "web_detail_list_page_type": "content_list",
        "web_detail_origin": "https://example.test",
        "web_detail_current_url": "https://example.test/admin/",
    }
    binding = _detail_before_action_binding(candidate_id=forged.candidate_id)
    binding["action_requested_url"] = forged.canonical_url
    binding["action_intent"] = {
        **binding["action_intent"],
        "parameters": {
            name: value
            for name, value in parameters.items()
            if name != "web_detail_before_action_capture_id"
        },
    }

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(
            snapshot_reader=DetailBindingReader(
                {
                    "capture-entry": snapshot,
                    "capture-before": {
                        **snapshot,
                        "capture_id": "capture-before",
                    },
                },
                binding=binding,
            )
        ).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="web:run-1",
                source_capture_id="capture-before",
                action_type=InteractionOperation.NAVIGATE,
                semantic_target="打开当前列表可信详情",
                parameters=parameters,
            )
        )

    assert error.value.error_code == "WEB_DETAIL_CANDIDATE_SOURCE_MISMATCH"


@pytest.mark.parametrize("surface_status", ["active", "draining"])
@pytest.mark.asyncio
async def test_detail_candidate_url_binds_discovery_capture_to_fresh_before_action_capture(
    surface_status,
) -> None:
    discovery = _detail_url_snapshot(
        capture_id="capture-entry",
        url="https://example.test/list/",
    )
    before = _detail_url_snapshot(
        capture_id="capture-before",
        url="https://example.test/list/",
    )
    candidate = detail_candidates_from_ready_snapshot(
        discovery,
        list_page_type="content_list",
        origin="https://example.test",
        current_url="https://example.test/list/",
    )[0]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=DetailBindingReader(
            {"capture-entry": discovery, "capture-before": before},
            binding=_detail_before_action_binding(
                candidate_id=candidate.candidate_id,
                changes={"surface_status": surface_status},
            ),
        )
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-1",
            source_capture_id="capture-before",
            action_type=InteractionOperation.NAVIGATE,
            semantic_target="打开当前列表可信详情",
            parameters=_detail_url_parameters(candidate.candidate_id),
        )
    )

    assert target.destination_url == "https://example.test/list/article/1"
    assert target.target_signature == candidate.candidate_id


@pytest.mark.asyncio
async def test_detail_candidate_url_accepts_discovery_cursor_until_command_release() -> None:
    discovery = _detail_url_snapshot(
        capture_id="capture-entry",
        url="https://example.test/list/",
    )
    before = _detail_url_snapshot(
        capture_id="capture-before",
        url="https://example.test/list/",
    )
    candidate = detail_candidates_from_ready_snapshot(
        discovery,
        list_page_type="content_list",
        origin="https://example.test",
        current_url="https://example.test/list/",
    )[0]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=DetailBindingReader(
            {"capture-entry": discovery, "capture-before": before},
            binding=_detail_before_action_binding(
                candidate_id=candidate.candidate_id,
                changes={"surface_current_capture_id": "capture-entry"},
            ),
        )
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-1",
            source_capture_id="capture-before",
            action_type=InteractionOperation.NAVIGATE,
            semantic_target="打开当前列表可信详情",
            parameters=_detail_url_parameters(candidate.candidate_id),
        )
    )

    assert target.destination_url == "https://example.test/list/article/1"
    assert target.target_signature == candidate.candidate_id


@pytest.mark.parametrize(
    ("case", "before_url", "binding_changes", "request_changes"),
    [
        ("missing_binding", "https://example.test/list/", None, {}),
        (
            "wrong_parent",
            "https://example.test/list/",
            {"parent_capture_id": "capture-other"},
            {},
        ),
        (
            "wrong_run",
            "https://example.test/list/",
            {"agent_run_id": "run-other", "parent_agent_run_id": "run-other"},
            {},
        ),
        (
            "wrong_task",
            "https://example.test/list/",
            {"task_id": "task-other"},
            {},
        ),
        (
            "wrong_session",
            "https://example.test/list/",
            {"session_id": "web:run-other"},
            {},
        ),
        (
            "cross_origin_before",
            "https://foreign.test/list/",
            {"final_url": "https://foreign.test/list/"},
            {},
        ),
        (
            "wrong_path_before",
            "https://example.test/admin/",
            {"final_url": "https://example.test/admin/"},
            {},
        ),
        (
            "not_current",
            "https://example.test/list/",
            {"surface_current_capture_id": "capture-other"},
            {},
        ),
        (
            "wrong_action_source",
            "https://example.test/list/",
            {"action_source_capture_id": "capture-other"},
            {},
        ),
        (
            "wrong_action_requested_source",
            "https://example.test/list/",
            {"action_requested_source_capture_id": "capture-other"},
            {},
        ),
        (
            "wrong_sequence",
            "https://example.test/list/",
            {"surface_command_sequence": 8},
            {},
        ),
        (
            "wrong_epoch",
            "https://example.test/list/",
            {"surface_finalization_epoch": 4},
            {},
        ),
        *(
            (
                f"surface_{status}",
                "https://example.test/list/",
                {"surface_status": status},
                {},
            )
            for status in ("starting", "closing", "closed", "lost", "failed")
        ),
        (
            "forged_before_id",
            "https://example.test/list/",
            {},
            {"web_detail_before_action_capture_id": "capture-other"},
        ),
    ],
)
@pytest.mark.asyncio
async def test_detail_candidate_url_rejects_untrusted_before_action_chain(
    case,
    before_url,
    binding_changes,
    request_changes,
) -> None:
    del case
    discovery = _detail_url_snapshot(
        capture_id="capture-entry",
        url="https://example.test/list/",
    )
    before = _detail_url_snapshot(capture_id="capture-before", url=before_url)
    candidate = detail_candidates_from_ready_snapshot(
        discovery,
        list_page_type="content_list",
        origin="https://example.test",
        current_url="https://example.test/list/",
    )[0]
    binding = (
        None
        if binding_changes is None
        else _detail_before_action_binding(
            candidate_id=candidate.candidate_id,
            changes=binding_changes,
        )
    )
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=DetailBindingReader(
            {"capture-entry": discovery, "capture-before": before},
            binding=binding,
        )
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await resolver.resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="web:run-1",
                source_capture_id="capture-before",
                action_type=InteractionOperation.NAVIGATE,
                semantic_target="打开当前列表可信详情",
                parameters={
                    **_detail_url_parameters(candidate.candidate_id),
                    **request_changes,
                },
            )
        )

    assert error.value.error_code == "WEB_DETAIL_CANDIDATE_SOURCE_MISMATCH"


@pytest.mark.parametrize("before_snapshot", [None, {"capture_id": "capture-drifted"}])
@pytest.mark.asyncio
async def test_detail_candidate_url_rejects_missing_or_drifted_before_snapshot(
    before_snapshot,
) -> None:
    discovery = _detail_url_snapshot(
        capture_id="capture-entry",
        url="https://example.test/list/",
    )
    candidate = detail_candidates_from_ready_snapshot(
        discovery,
        list_page_type="content_list",
        origin="https://example.test",
        current_url="https://example.test/list/",
    )[0]

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(
            snapshot_reader=DetailBindingReader(
                {"capture-entry": discovery, "capture-before": before_snapshot},
                binding=_detail_before_action_binding(candidate_id=candidate.candidate_id),
            )
        ).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="web:run-1",
                source_capture_id="capture-before",
                action_type=InteractionOperation.NAVIGATE,
                semantic_target="打开当前列表可信详情",
                parameters=_detail_url_parameters(candidate.candidate_id),
            )
        )

    assert error.value.error_code in {
        "WEB_DETAIL_CANDIDATE_SOURCE_MISMATCH",
        "WEB_DETAIL_SNAPSHOT_CAPTURE_MISMATCH",
    }


@pytest.mark.asyncio
async def test_binds_missing_capture_id_from_trusted_snapshot_lookup_for_detail_url() -> None:
    stored_snapshot = {
        "url": "https://example.test/list/",
        "structure_signature": "signature-detail-list",
        "elements": [
            {
                "selector": "#card-1 a",
                "tag": "a",
                "role": "link",
                "text": "内容标题",
                "href": "article/1",
                "rect": {"x": 100, "y": 1200, "width": 300, "height": 180},
                "interactive": True,
                "visible": True,
                "hit_test": {"center_hits_target": False},
                "within_repeated_structure": True,
                "ancestor_class_tokens": ["article-card", "content-grid"],
            }
        ],
    }
    bound_snapshot = {**stored_snapshot, "capture_id": "capture-entry"}
    candidate = detail_candidates_from_ready_snapshot(
        bound_snapshot,
        list_page_type="content_list",
        origin="https://example.test",
        current_url="https://example.test/list/",
    )[0]

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=DetailBindingReader(
            {
                "capture-entry": stored_snapshot,
                "capture-before": {
                    **bound_snapshot,
                    "capture_id": "capture-before",
                },
            },
            binding=_detail_before_action_binding(candidate_id=candidate.candidate_id),
        )
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-1",
            source_capture_id="capture-before",
            action_type=InteractionOperation.NAVIGATE,
            semantic_target="打开当前列表可信详情",
            parameters=_detail_url_parameters(candidate.candidate_id),
        )
    )

    assert target.destination_url == candidate.canonical_url
    assert target.target_signature == candidate.candidate_id


@pytest.mark.asyncio
async def test_rejects_conflicting_capture_id_from_trusted_snapshot_lookup() -> None:
    stored_snapshot = {
        "capture_id": "capture-other",
        "structure_signature": "signature-detail-list",
        "elements": [],
    }

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(
            snapshot_reader=SnapshotReader(stored_snapshot)
        ).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="web:run-1",
                source_capture_id="capture-before",
                action_type=InteractionOperation.NAVIGATE,
                semantic_target="打开当前列表可信详情",
                parameters={
                    "target_mode": "web_detail_candidate_url",
                    "destination_url": "https://example.test/article/1",
                    "web_detail_candidate_id": "web-candidate:sealed",
                    "web_detail_source_capture_id": "capture-entry",
                    "web_detail_before_action_capture_id": "capture-before",
                    "web_detail_list_page_type": "content_list",
                    "web_detail_origin": "https://example.test",
                    "web_detail_current_url": "https://example.test/list",
                },
            )
        )

    assert error.value.error_code == "WEB_DETAIL_SNAPSHOT_CAPTURE_MISMATCH"


@pytest.mark.parametrize(
    "stored_capture_id",
    [False, 0, None, "", " capture-entry "],
)
@pytest.mark.asyncio
async def test_rejects_explicit_invalid_snapshot_capture_id(
    stored_capture_id: object,
) -> None:
    stored_snapshot = {
        "capture_id": stored_capture_id,
        "structure_signature": "signature-detail-list",
        "elements": [],
    }

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(
            snapshot_reader=SnapshotReader(stored_snapshot)
        ).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="web:run-1",
                source_capture_id="capture-before",
                action_type=InteractionOperation.NAVIGATE,
                semantic_target="打开当前列表可信详情",
                parameters={
                    "target_mode": "web_detail_candidate_url",
                    "destination_url": "https://example.test/article/1",
                    "web_detail_candidate_id": "web-candidate:sealed",
                    "web_detail_source_capture_id": "capture-entry",
                    "web_detail_before_action_capture_id": "capture-before",
                    "web_detail_list_page_type": "content_list",
                    "web_detail_origin": "https://example.test",
                    "web_detail_current_url": "https://example.test/list",
                },
            )
        )

    assert error.value.error_code == "WEB_DETAIL_SNAPSHOT_CAPTURE_MISMATCH"


@pytest.mark.asyncio
async def test_rebinds_detail_candidate_on_fresh_before_capture() -> None:
    original_snapshot = {
        "capture_id": "capture-ready",
        "structure_signature": "signature-detail-list",
        "elements": [
            {
                "selector": "#card-1 a",
                "tag": "a",
                "role": "link",
                "text": "内容标题",
                "href": "/article/1",
                "rect": {"x": 100, "y": 200, "width": 300, "height": 180},
                "interactive": True,
                "visible": True,
                "hit_test": {"center_hits_target": True},
                "within_repeated_structure": True,
                "ancestor_class_tokens": ["article-card", "content-grid"],
            }
        ],
    }
    fresh_snapshot = {
        **original_snapshot,
        "capture_id": "capture-fresh-before",
        "structure_signature": "signature-after-scroll",
        "elements": [
            {
                **original_snapshot["elements"][0],
                "selector": "main article:nth-of-type(1) a",
                "rect": {"x": 100, "y": 220, "width": 300, "height": 180},
            }
        ],
    }
    selected = detail_candidates_from_ready_snapshot(
        original_snapshot,
        list_page_type="content_list",
        origin="https://example.test",
    )[0]

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=SnapshotReader(
            {
                "capture-ready": original_snapshot,
                "capture-fresh-before": fresh_snapshot,
            },
            expected_capture_id="capture-fresh-before",
        )
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="web:run-1",
            source_capture_id="capture-fresh-before",
            action_type=InteractionOperation.CLICK,
            semantic_target="打开当前列表可信详情",
            parameters={
                "target_mode": "web_detail_candidate",
                "web_detail_candidate_id": selected.candidate_id,
                "web_detail_source_capture_id": "capture-ready",
                "web_detail_list_page_type": "content_list",
                "web_detail_origin": "https://example.test",
            },
        )
    )

    assert target.selector == "main article:nth-of-type(1) a"
    assert target.target_signature == selected.candidate_id
