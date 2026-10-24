"""H5 SPA / Flutter Web 逻辑页面证据、指纹与信任域测试。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from services.api.app.application.h5_logical_surface_evidence import (
    build_h5_logical_surface,
)
from services.api.app.domain.exploration.h5_logical_surface import (
    bind_h5_page_type,
    compare_h5_logical_surfaces,
    stable_h5_surface_hash,
)

URL = "https://example.test/"


@pytest.mark.parametrize("destination", ["/video", "/ai", "/#/library"])
def test_same_layout_route_navigation_binds_actual_captured_destination(destination):
    before = _build(_spa_snapshot(), url=URL)
    after = _build(_spa_snapshot(), url=f"https://example.test{destination}")
    proof = {
        "protocol_version": "bottom_navigation_transition_proof.v1",
        "status": "trusted", "effect": "same_origin_url_change",
        "before_location": URL,
        "after_location": f"https://example.test{destination}",
        "navigation_family": "footer_wrap", "navigation_index": 2,
        "before_identity_fingerprint": before["identity_fingerprint"],
        "after_identity_fingerprint": after["identity_fingerprint"],
    }
    assert before["identity_fingerprint"] != after["identity_fingerprint"]
    transition = compare_h5_logical_surfaces(
        before=before, after=after, bottom_navigation_proof=proof,
    )
    assert transition["logical_transition_proof"] == "trusted"
    assert transition["transition_kind"] == "logical_page_changed"
    forged = compare_h5_logical_surfaces(
        before=before, after=after,
        bottom_navigation_proof={**proof, "after_location": "https://example.test/unvisited"},
    )
    assert forged["logical_transition_proof"] != "trusted"


def test_dynamic_query_and_ordinary_anchor_do_not_change_route_identity():
    before = _build(_spa_snapshot(), url=URL + "?timestamp=123#notice")
    after = _build(_spa_snapshot(), url=URL + "?timestamp=456#footer")
    assert before["identity_fingerprint"] == after["identity_fingerprint"]


@pytest.mark.parametrize("before_query,after_query", [
    ("?page=video", "?page=comic"),
    ("?id=12", "?id=34"),
    ("#/viewer?id=12", "#/viewer?id=34"),
])
def test_business_query_routes_are_not_merged(before_query, after_query):
    before = _build(_spa_snapshot(), url=URL + before_query)
    after = _build(_spa_snapshot(), url=URL + after_query)
    assert before["identity_fingerprint"] != after["identity_fingerprint"]


def test_reused_modal_node_with_new_media_changes_state_not_business_identity():
    snapshot = _spa_snapshot()
    snapshot["elements"].append(_element(
        selector="#notice", tag="div", role="dialog", class_tokens=["modal"],
    ))
    snapshot["elements"][-1]["h5_modal_media_identity"] = ["sha256:" + "a" * 64]
    after_snapshot = deepcopy(snapshot)
    after_snapshot["elements"][-1]["h5_modal_media_identity"] = ["sha256:" + "b" * 64]
    before, after = _build(snapshot), _build(after_snapshot)
    assert before["identity_fingerprint"] == after["identity_fingerprint"]
    assert before["state_fingerprint"] != after["state_fingerprint"]
    assert compare_h5_logical_surfaces(before=before, after=after)["transition_kind"] == (
        "surface_state_changed"
    )


@pytest.mark.parametrize("reverse_items", [False, True])
def test_repeated_list_sampling_does_not_change_logical_identity(reverse_items: bool) -> None:
    def snapshot(first_count: int, second_count: int, scroll_y: int) -> dict[str, Any]:
        items = [
            _element(selector=f"#stable-{index}", tag="a", text=f"固定入口 {index}",
                     class_tokens=["a-control"], interactive=True)
            for index in range(48)
        ]
        items.extend(
            _element(selector=f"#card-{index}", tag="a", text="内容卡片",
                     class_tokens=["b-card"], interactive=True)
            for index in range(first_count)
        )
        items.extend(
            _element(selector=f"#title-{index}", tag="a", text="内容标题",
                     class_tokens=["c-title"], interactive=True)
            for index in range(second_count)
        )
        return {
            "ready_state": "complete",
            "document": {"flutter_root_present": False},
            "viewport": {"width": 390, "height": 844, "scroll_y": scroll_y},
            "elements": list(reversed(items)) if reverse_items else items,
        }

    def surface(value: dict[str, Any]) -> dict[str, Any]:
        return build_h5_logical_surface(
            ui_snapshot=value, page_url=URL, surface_session_id="h5:test",
            document_token="same-document",
        )

    before = surface(snapshot(10, 14, 0))
    after_snapshot = snapshot(5, 19, 1434)
    after = surface(after_snapshot)
    assert before["identity_fingerprint"] == after["identity_fingerprint"]
    assert before["state_fingerprint"] != after["state_fingerprint"]
    assert compare_h5_logical_surfaces(before=before, after=after)["transition_kind"] == (
        "surface_state_changed"
    )

    # 新结构类型与新的业务标题仍必须改变身份，不能把所有列表合成一页。
    changed_structure = deepcopy(after_snapshot)
    changed_structure["elements"].append(
        _element(selector="#new-form", tag="button", text="新表单",
                 class_tokens=["new-structure"], interactive=True)
    )
    assert surface(changed_structure)["identity_fingerprint"] != after["identity_fingerprint"]
    changed_heading = deepcopy(after_snapshot)
    changed_heading["elements"].append(
        _element(selector="h1", tag="h1", text="另一业务栏目")
    )
    assert surface(changed_heading)["identity_fingerprint"] != after["identity_fingerprint"]


@pytest.mark.parametrize(
    "failure_key", ["collection_error", "capture_degraded", "budget_truncated"]
)
def test_ready_native_dom_establishes_identity_without_spa_framework(failure_key: str) -> None:
    snapshot = {
        "schema_version": "ui-snapshot.v2",
        "ready_state": "complete",
        "flutter_semantics_activation": "not_applicable",
        "document": {"time_origin_ms": 1000, "flutter_root_present": False},
        "elements": [
            _element(selector="h1", tag="h1", text="栏目首页"),
            _element(selector="#next", tag="a", text="下一栏目", interactive=True),
        ],
    }
    first = build_h5_logical_surface(
        ui_snapshot=snapshot,
        page_url=URL,
        surface_session_id="h5:test",
        document_token="document-1",
    )
    assert first["render_profile"]["kind"] == "dom_document"
    assert first["render_profile"]["status"] == "confirmed"
    assert first["evidence_quality"] == "high"
    from services.api.app.domain.exploration.page_graph import normalize_page_identity

    identity = normalize_page_identity(
        metadata={"logical_surface": first},
        surface_kind="web",
        capture_id="capture-1",
    )
    assert identity.status == "confirmed"
    assert identity.source == "logical_surface"
    snapshot[failure_key] = True
    degraded = build_h5_logical_surface(
        ui_snapshot=snapshot,
        page_url=URL,
        surface_session_id="h5:test",
        document_token="document-1",
    )
    assert degraded["render_profile"]["status"] != "confirmed"


def _element(
    *,
    selector: str,
    tag: str,
    text: str = "",
    role: str | None = None,
    class_tokens: list[str] | None = None,
    parent_class_tokens: list[str] | None = None,
    ancestor_class_tokens: list[str] | None = None,
    structural_navigation_kind: str | None = None,
    interactive: bool = False,
    rect: dict[str, float] | None = None,
    fixed_or_sticky_ancestor: bool = False,
    viewport_coverage: float | None = None,
    within_navigation: bool = False,
) -> dict[str, Any]:
    return {
        "selector": selector,
        "tag": tag,
        "text": text,
        "role": role,
        "class_tokens": class_tokens or [],
        "parent_class_tokens": parent_class_tokens or [],
        "ancestor_class_tokens": ancestor_class_tokens or [],
        "structural_navigation_kind": structural_navigation_kind,
        "interactive": interactive,
        "visible": True,
        "rect": rect or {"x": 0, "y": 0, "width": 100, "height": 40},
        "fixed_or_sticky_ancestor": fixed_or_sticky_ancestor,
        "viewport_coverage": viewport_coverage,
        "within_navigation": within_navigation,
        "hit_test": {"center_hits_target": True},
    }


def _flutter_snapshot(*, node_prefix: str = "flt-100") -> dict[str, Any]:
    return {
        "schema_version": "ui-snapshot.v2",
        "url": URL,
        "title": "测试站",
        "flutter_semantics_activation": "enabled",
        "document": {"history_api_observed": True},
        "elements": [
            _element(
                selector=f"flt-semantics#{node_prefix}-home",
                tag="flt-semantics",
                text="首页",
                role="button",
                class_tokens=[f"semantics-{node_prefix}", "active"],
                structural_navigation_kind="flutter_bottom_navigation",
                interactive=True,
                rect={"x": 0, "y": 720, "width": 90, "height": 60},
            ),
            _element(
                selector=f"flt-semantics#{node_prefix}-profile",
                tag="flt-semantics",
                text="我的",
                role="button",
                class_tokens=[f"semantics-{node_prefix}"],
                structural_navigation_kind="flutter_bottom_navigation",
                interactive=True,
                rect={"x": 270, "y": 720, "width": 90, "height": 60},
            ),
            _element(
                selector=f"flt-semantics#{node_prefix}-heading",
                tag="flt-semantics",
                text="首页推荐",
                role="heading",
                class_tokens=[f"semantics-{node_prefix}"],
            ),
        ],
    }


def _flutter_snapshot_with_centered_overlay(*, overlay: bool) -> dict[str, Any]:
    snapshot = _flutter_snapshot()
    snapshot["viewport"] = {"width": 390, "height": 844, "scroll_y": 0}
    snapshot["elements"].insert(
        0,
        _element(
            selector="flt-semantics#root-group",
            tag="flt-semantics",
            text="首页结构",
            role="group",
            rect={"x": 0, "y": 0, "width": 390, "height": 844},
        ),
    )
    if overlay:
        snapshot["elements"].extend(
            [
                _element(
                    selector="flt-semantics#flt-semantic-node-701",
                    tag="flt-semantics",
                    interactive=True,
                    rect={"x": 36, "y": 216, "width": 318, "height": 354},
                ),
                _element(
                    selector="flt-semantics#flt-semantic-node-702",
                    tag="flt-semantics",
                    interactive=True,
                    rect={"x": 177, "y": 591, "width": 36, "height": 36},
                ),
            ]
        )
    return snapshot


def _spa_snapshot(*, page: str = "home", modal: bool = False) -> dict[str, Any]:
    is_home = page == "home"
    elements = [
        _element(
            selector="#nav-home",
            tag="button",
            text="首页",
            role="tab",
            class_tokens=["tab", *(("active",) if is_home else ())],
            structural_navigation_kind="bottom_navigation",
            interactive=True,
            rect={"x": 0, "y": 720, "width": 90, "height": 60},
        ),
        _element(
            selector="#nav-profile",
            tag="button",
            text="我的",
            role="tab",
            class_tokens=["tab", *(("active",) if not is_home else ())],
            structural_navigation_kind="bottom_navigation",
            interactive=True,
            rect={"x": 270, "y": 720, "width": 90, "height": 60},
        ),
        _element(
            selector="#page-heading",
            tag="h1",
            text="首页推荐" if is_home else "个人中心",
            role="heading",
            class_tokens=["page-heading"],
        ),
        _element(
            selector="#primary-action",
            tag="button",
            text="搜索" if is_home else "设置",
            role="button",
            class_tokens=["primary-action"],
            interactive=True,
        ),
    ]
    if modal:
        elements.extend(
            [
                _element(
                    selector="#notice-modal",
                    tag="div",
                    text="活动提示 2026-08-28",
                    role="dialog",
                    class_tokens=["notice-modal", "overlay"],
                ),
                _element(
                    selector="#notice-modal-close",
                    tag="button",
                    text="关闭",
                    role="button",
                    class_tokens=["modal-close"],
                    interactive=True,
                ),
            ]
        )
    return {
        "schema_version": "ui-snapshot.v2",
        "url": URL,
        "title": "测试站",
        "flutter_semantics_activation": "not_available",
        "document": {
            "history_api_observed": True,
            "spa_root_present": True,
            "navigation_type": "navigate",
        },
        "viewport": {"width": 360, "height": 780, "scroll_y": 0},
        "elements": elements,
    }


def _pages_foot_sparse_snapshot(*, active_index: int) -> dict[str, Any]:
    """复现站点 5 无原生 role/heading 的自定义底栏结构。"""
    labels = ("首页", "社区", "二次元", "专区", "AI", "我的")
    return {
        "schema_version": "ui-snapshot.v2",
        "url": URL,
        "title": "抖阴精选",
        "flutter_semantics_activation": "not_available",
        "document": {
            "history_api_observed": True,
            "spa_root_present": True,
            "navigation_type": "navigate",
        },
        "viewport": {"width": 390, "height": 844, "scroll_y": 0},
        "elements": [
            _element(
                selector=(
                    "#app > div > ul.pages-foot-fix > "
                    f"li:nth-of-type({index})"
                ),
                tag="li",
                text=label,
                class_tokens=[
                    "flex-column",
                    "font12",
                    *(('active',) if index == active_index else ()),
                    ("mine" if index == 6 else "ai" if index == 5 else "home"),
                ],
                parent_class_tokens=["pages-foot-fix", "justify-around"],
                ancestor_class_tokens=["pages-foot-fix", "pages-foot-box"],
                interactive=False,
                rect={
                    "x": float((index - 1) * 65),
                    "y": 780.0,
                    "width": 65.0,
                    "height": 64.0,
                },
            )
            for index, label in enumerate(labels, start=1)
        ],
    }


def _build(snapshot: dict[str, Any], *, url: str = URL) -> dict[str, Any]:
    return build_h5_logical_surface(
        ui_snapshot=snapshot,
        page_url=url,
        surface_session_id="session-1",
        document_token="document-1",
    )


def _visual_layout_evidence(
    *,
    main_layout: str = "grid",
    active_slot: int | None = 0,
    animation_bucket: str = "none",
) -> dict[str, Any]:
    return {
        "protocol_version": "visual_layout_evidence.v1",
        "identity": {
            "top_structure": {"category": "app_bar", "segment_count": 1},
            "bottom_navigation": {
                "slot_count": 4,
                "active_slot": active_slot,
            },
            "main_layout_category": main_layout,
        },
        "state_audit": {
            "main_density_bucket": "medium",
            "animation_bucket": animation_bucket,
            "full_frame_change_bucket": "not_compared",
        },
    }


def _with_visual_layout(
    snapshot: dict[str, Any],
    *,
    main_layout: str = "grid",
    active_slot: int | None = 0,
    animation_bucket: str = "none",
) -> dict[str, Any]:
    snapshot["visual_navigation_capture"] = {
        "protocol_version": "visual_navigation_capture.v1",
        "purpose": "visual_navigation_viewport",
        "coordinate_space": "visual_viewport_css_pixels",
        "screenshot_index": 2,
    }
    snapshot["visual_layout_evidence"] = _visual_layout_evidence(
        main_layout=main_layout,
        active_slot=active_slot,
        animation_bucket=animation_bucket,
    )
    return snapshot


def test_flutter_dynamic_node_ids_do_not_change_identity() -> None:
    first = _build(_flutter_snapshot(node_prefix="flt-101"))
    second = _build(_flutter_snapshot(node_prefix="flt-901"))

    assert first["render_profile"]["kind"] == "flutter_web"
    assert first["render_profile"]["status"] == "confirmed"
    assert first["identity_fingerprint"] == second["identity_fingerprint"]
    assert first["state_fingerprint"] == second["state_fingerprint"]


def test_flutter_semantics_coverage_is_evidence_not_identity_material() -> None:
    """滚动、节点数量和兄弟顺序变化只能改变状态或证据质量。"""
    first_snapshot = _flutter_snapshot(node_prefix="flt-101")
    first_snapshot["viewport"] = {"width": 390, "height": 844, "scroll_y": 0}
    first_snapshot["ocr_text"] = "首页视频排行帖子我的设置签到活动支付会员中心"
    second_snapshot = _flutter_snapshot(node_prefix="flt-901")
    second_snapshot["viewport"] = {"width": 390, "height": 844, "scroll_y": 480}
    second_snapshot["ocr_text"] = first_snapshot["ocr_text"]
    second_snapshot["elements"] = list(reversed(second_snapshot["elements"])) + [
        _element(
            selector="flt-semantics#flt-semantic-node-999",
            tag="flt-semantics",
            text="活动入口",
            role="button",
            interactive=True,
        )
    ]

    first = _build(first_snapshot)
    second = _build(second_snapshot)

    assert first["identity_fingerprint"] == second["identity_fingerprint"]
    assert first["state_fingerprint"] != second["state_fingerprint"]
    assert (
        first["semantics_coverage"]["usable_node_count"]
        != second["semantics_coverage"]["usable_node_count"]
    )


def test_flutter_semantics_rebuild_across_node_count_bucket_keeps_identity() -> None:
    """Flutter 节点重建跨越计数桶时，逻辑页面身份仍须保持稳定。"""
    first_snapshot = _flutter_snapshot(node_prefix="flt-101")
    second_snapshot = _flutter_snapshot(node_prefix="flt-901")
    second_snapshot["elements"].extend(
        _element(
            selector=f"flt-semantics#flt-semantic-node-{index}",
            tag="flt-semantics",
            text=f"重建节点{index}",
            role="button",
            interactive=True,
        )
        for index in range(18)
    )

    first = _build(first_snapshot)
    second = _build(second_snapshot)

    assert first["identity_fingerprint"] == second["identity_fingerprint"]
    assert first["state_signals"]["semantics_coverage"]["usable_node_count"] == 3
    assert second["state_signals"]["semantics_coverage"]["usable_node_count"] == 21


def test_visual_layout_identity_is_gated_to_confirmed_sparse_flutter() -> None:
    """普通 H5、DOM SPA 与 Semantics 充足 Flutter 不得新增视觉身份。"""
    sparse = _build(_with_visual_layout(_flutter_snapshot()))
    ordinary = _build(_with_visual_layout(_spa_snapshot()))
    sufficient_snapshot = _with_visual_layout(_flutter_snapshot())
    sufficient_snapshot["ocr_text"] = "首页视频排行帖子我的设置签到活动支付内容详情推荐专题热门最新"
    sufficient_snapshot["elements"].extend(
        _element(
            selector=f"flt-semantics#sufficient-{index}",
            tag="flt-semantics",
            text=f"稳定语义入口{index}",
            role="button",
            interactive=True,
        )
        for index in range(12)
    )
    sufficient = _build(sufficient_snapshot)

    assert sparse["semantics_coverage"]["level"] == "semantics_sparse"
    assert sparse["identity_signals"]["visual_layout"] == (
        _visual_layout_evidence()["identity"]
    )
    assert "visual_layout" in sparse["evidence_sources"]
    assert "visual_layout" not in ordinary["identity_signals"]
    assert sufficient["semantics_coverage"]["level"] == "semantics_sufficient"
    assert "visual_layout" not in sufficient["identity_signals"]


def test_visual_layout_state_audit_and_flutter_rebuild_do_not_change_identity() -> None:
    """整帧变化、动画、滚动、兄弟顺序与节点集合只能进入 state/audit。"""
    before_snapshot = _with_visual_layout(
        _flutter_snapshot(node_prefix="flt-101"),
        animation_bucket="none",
    )
    before_snapshot["viewport"] = {"width": 390, "height": 844, "scroll_y": 0}
    after_snapshot = _with_visual_layout(
        _flutter_snapshot(node_prefix="flt-901"),
        animation_bucket="present",
    )
    after_snapshot["viewport"] = {"width": 390, "height": 844, "scroll_y": 480}
    after_snapshot["elements"] = list(reversed(after_snapshot["elements"])) + [
        _element(
            selector="flt-semantics#rebuilt-extra",
            tag="flt-semantics",
            text="动态兄弟节点",
            role="button",
            interactive=True,
        )
    ]
    after_snapshot["visual_layout_evidence"]["state_audit"].update(
        {
            "full_frame_change_bucket": "large",
            "color_change_bucket": "large",
            "node_set_bucket": "rebuilt",
        }
    )

    before = _build(before_snapshot)
    after = _build(after_snapshot)

    assert before["identity_fingerprint"] == after["identity_fingerprint"]
    assert before["state_fingerprint"] != after["state_fingerprint"]
    serialized_identity = str(before["identity_signals"]["visual_layout"])
    for forbidden in ("full_frame", "animation", "color", "node_set", "scroll"):
        assert forbidden not in serialized_identity


def test_sparse_flutter_visual_proof_is_independent_primary_transition_evidence() -> None:
    """视觉布局变化只有受信两帧主证明时才可形成逻辑页面转换。"""
    before = _build(
        _with_visual_layout(
            _flutter_snapshot(),
            main_layout="grid",
            active_slot=0,
        )
    )
    after = _build(
        _with_visual_layout(
            _flutter_snapshot(),
            main_layout="detail",
            active_slot=0,
        )
    )
    safe_proof = {
        "protocol_version": "visual_navigation_transition_proof.v1",
        "status": "trusted",
        "after_frame_count": 2,
        "after_frames_stable": True,
        "main_structure_changed": True,
        "destination_structure": "detail",
        "after_identity_bound": True,
        "reason_codes": [],
    }

    without_visual = compare_h5_logical_surfaces(before=before, after=after)
    with_visual = compare_h5_logical_surfaces(
        before=before,
        after=after,
        visual_proof=safe_proof,
    )

    assert without_visual["logical_transition_proof"] != "trusted"
    assert with_visual["transition_kind"] == "logical_page_changed"
    assert with_visual["logical_transition_proof"] == "trusted"
    assert with_visual["visual_proof"] == safe_proof
    assert "visual_layout" in with_visual["corroborating_signals"]


def test_failed_visual_proof_cannot_be_overridden_by_document_or_dom_signals() -> None:
    """视觉候选的 URL/history/DOM 辅证不能覆盖视觉稳定性失败。"""
    before = _build(
        _with_visual_layout(_flutter_snapshot(), main_layout="grid", active_slot=0)
    )
    after = _build(
        _with_visual_layout(_flutter_snapshot(), main_layout="detail", active_slot=0),
        url="https://example.test/detail",
    )
    after["document_identity"] = "sha256:" + "9" * 64
    after["identity_signals"]["page_heading"] = [{"label": "详情"}]
    after["identity_signals"]["main_structure"] = [{"role": "article"}]
    failed_proof = {
        "protocol_version": "visual_navigation_transition_proof.v1",
        "status": "insufficient",
        "after_frame_count": 2,
        "after_frames_stable": False,
        "main_structure_changed": True,
        "after_identity_bound": True,
        "reason_codes": ["after_frames_inconsistent"],
    }

    transition = compare_h5_logical_surfaces(
        before=before,
        after=after,
        visual_proof=failed_proof,
    )

    assert transition["logical_transition_proof"] != "trusted"
    assert transition["transition_kind"] == "uncertain"
    assert transition["visual_proof"] == failed_proof


@pytest.mark.parametrize(
    ("role", "structural_navigation_kind", "interactive"),
    [
        pytest.param("button", "flutter_bottom_navigation", True, id="navigation"),
        pytest.param("heading", None, False, id="heading"),
        pytest.param("button", None, True, id="stable-control"),
    ],
)
def test_flutter_rebuild_across_cardinality_buckets_keeps_identity(
    role: str,
    structural_navigation_kind: str | None,
    interactive: bool,
) -> None:
    """Flutter 导航、标题和控件重建跨桶时均不得改变逻辑身份。"""
    first_snapshot = _flutter_snapshot(node_prefix="flt-101")
    second_snapshot = _flutter_snapshot(node_prefix="flt-901")
    second_snapshot["elements"].extend(
        _element(
            selector=f"flt-semantics#flutter-rebuild-{index}",
            tag="flt-semantics",
            text=f"重建节点{index}",
            role=role,
            structural_navigation_kind=structural_navigation_kind,
            interactive=interactive,
        )
        for index in range(19)
    )

    first = _build(first_snapshot)
    second = _build(second_snapshot)

    assert first["identity_fingerprint"] == second["identity_fingerprint"]
    assert first["identity_signals"]["active_navigation"] == second["identity_signals"][
        "active_navigation"
    ]
    assert first["identity_signals"]["page_heading"] == second["identity_signals"][
        "page_heading"
    ]
    assert first["identity_signals"]["stable_controls"] == second["identity_signals"][
        "stable_controls"
    ]


def test_flutter_centered_overlay_changes_state_but_not_identity() -> None:
    before = _build(_flutter_snapshot_with_centered_overlay(overlay=True))
    after = _build(_flutter_snapshot_with_centered_overlay(overlay=False))

    transition = compare_h5_logical_surfaces(before=before, after=after)

    assert before["identity_fingerprint"] == after["identity_fingerprint"]
    assert before["state_fingerprint"] != after["state_fingerprint"]
    assert before["state_signals"]["obstruction_count"] == "2-5"
    assert after["state_signals"]["obstruction_count"] == "0"
    assert transition["transition_kind"] == "surface_state_changed"


def test_selected_page_tab_changes_state_but_not_page_identity() -> None:
    before_snapshot = _spa_snapshot()
    after_snapshot = deepcopy(before_snapshot)
    before_snapshot["elements"].extend(
        [
            _element(
                selector="#hometab .van-tab:nth-of-type(1)",
                tag="div",
                text="推荐",
                role="tab",
                class_tokens=["van-tab", "van-tab--active"],
                interactive=True,
            ),
            _element(
                selector="#hometab .van-tab:nth-of-type(2)",
                tag="div",
                text="国产",
                role="tab",
                class_tokens=["van-tab"],
                interactive=True,
            ),
        ]
    )
    after_snapshot["elements"].extend(
        [
            _element(
                selector="#hometab .van-tab:nth-of-type(1)",
                tag="div",
                text="推荐",
                role="tab",
                class_tokens=["van-tab"],
                interactive=True,
            ),
            _element(
                selector="#hometab .van-tab:nth-of-type(2)",
                tag="div",
                text="国产",
                role="tab",
                class_tokens=["van-tab", "van-tab--active"],
                interactive=True,
            ),
        ]
    )

    before = _build(before_snapshot)
    after = _build(after_snapshot)
    transition = compare_h5_logical_surfaces(before=before, after=after)

    assert before["identity_fingerprint"] == after["identity_fingerprint"]
    assert before["state_fingerprint"] != after["state_fingerprint"]
    assert before["state_signals"]["selected_tab"] == "推荐"
    assert after["state_signals"]["selected_tab"] == "国产"
    assert transition["transition_kind"] == "surface_state_changed"
    assert transition["logical_transition_proof"] == "trusted"


def test_horizontal_page_tab_motion_does_not_create_new_page_identity() -> None:
    before_snapshot = _spa_snapshot()
    before_snapshot["elements"].extend(
        [
            _element(
                selector=f"#categories .van-tab:nth-of-type({index})",
                tag="div",
                text=label,
                role="tab",
                class_tokens=["van-tab", *(["van-tab--active"] if index == 1 else [])],
                interactive=True,
                rect={"x": x, "y": 100, "width": 80, "height": 36},
            )
            for index, (label, x) in enumerate(
                (("推荐", 10), ("国产", 100), ("动漫", 190)), start=1
            )
        ]
    )
    after_snapshot = deepcopy(before_snapshot)
    for item in after_snapshot["elements"][-3:]:
        item["rect"]["x"] += 23

    before = _build(before_snapshot)
    after = _build(after_snapshot)

    assert before["identity_fingerprint"] == after["identity_fingerprint"]
    assert before["state_signals"]["selected_tab"] == "推荐"
    assert after["state_signals"]["selected_tab"] == "推荐"


def test_flutter_content_card_is_not_promoted_to_surface_obstruction() -> None:
    snapshot = _flutter_snapshot_with_centered_overlay(overlay=False)
    snapshot["elements"].append(
        _element(
            selector="flt-semantics#content-card",
            tag="flt-semantics",
            interactive=True,
            rect={"x": 12, "y": 80, "width": 120, "height": 180},
        )
    )

    surface = _build(snapshot)

    assert surface["state_signals"]["obstruction_count"] == "0"


def test_modal_changes_surface_state_but_not_logical_identity() -> None:
    before = _build(_spa_snapshot())
    after = _build(_spa_snapshot(modal=True))

    transition = compare_h5_logical_surfaces(before=before, after=after)

    assert before["identity_fingerprint"] == after["identity_fingerprint"]
    assert before["state_fingerprint"] != after["state_fingerprint"]
    assert transition["transition_kind"] == "surface_state_changed"
    assert transition["logical_transition_proof"] == "trusted"


def test_document_modal_open_flag_does_not_turn_business_nodes_into_obstructions() -> None:
    baseline = _spa_snapshot()
    opened = deepcopy(baseline)
    for element in opened["elements"]:
        element.setdefault("ancestor_class_tokens", []).append("modal-open")
    before = _build(baseline)
    after = _build(opened)
    assert before["identity_fingerprint"] == after["identity_fingerprint"]
    assert after["state_signals"]["obstruction_count"] == "0"


def test_document_business_layout_shift_is_not_new_page_identity() -> None:
    baseline = _spa_snapshot()
    shifted = deepcopy(baseline)
    for element in shifted["elements"]:
        element["rect"]["y"] += 70
    assert _build(baseline)["identity_fingerprint"] == _build(shifted)["identity_fingerprint"]


def test_incidental_floating_control_does_not_change_dom_spa_identity() -> None:
    """非遮挡浮动入口的出现或消失不能让同一业务页的导航候选过期。"""
    without_floating = _spa_snapshot()
    with_floating = deepcopy(without_floating)
    with_floating["elements"].append(
        _element(
            selector="#floating-entry > button",
            tag="button",
            text="AI 爽剧来袭",
            class_tokens=["ai-drama-float-bubble__hit"],
            parent_class_tokens=["ai-drama-float-bubble"],
            ancestor_class_tokens=["ai-drama-float-bubble", "main-wrapper"],
            interactive=True,
            rect={"x": 43, "y": 745, "width": 136, "height": 35},
            fixed_or_sticky_ancestor=True,
            viewport_coverage=0.015,
        )
    )

    before = _build(with_floating)
    after = _build(without_floating)

    assert before["identity_fingerprint"] == after["identity_fingerprint"]
    assert before["identity_signals"] == after["identity_signals"]


def test_obstruction_clearance_tolerates_unanchored_structure_drift() -> None:
    """弹窗退场时底层动态 Tab 抖动不得把同页状态变化误判成新页面。"""
    before_snapshot = _spa_snapshot(modal=True)
    after_snapshot = _spa_snapshot(modal=False)
    for index in range(22):
        before_snapshot["elements"].append(
            _element(
                selector=f"#dynamic-tab-before-{index}",
                tag="div",
                text=f"频道 {index}",
                role="tab",
                interactive=True,
                rect={"x": index * 24, "y": 48, "width": 48, "height": 24},
            )
        )
    for index in range(24):
        after_snapshot["elements"].append(
            _element(
                selector=f"#dynamic-tab-after-{index}",
                tag="div",
                text=f"频道 {index}",
                role="tab",
                # 数量波动不再改变身份；额外结构类型仍覆盖真实的非锚点漂移。
                class_tokens=["dynamic-tab-wrapper"] if index == 23 else [],
                interactive=True,
                rect={"x": index * 22, "y": 48, "width": 48, "height": 24},
            )
        )

    before = _build(before_snapshot)
    after = _build(after_snapshot)
    transition = compare_h5_logical_surfaces(before=before, after=after)

    assert before["identity_fingerprint"] != after["identity_fingerprint"]
    assert before["state_signals"]["obstruction_count"] != "0"
    assert after["state_signals"]["obstruction_count"] == "0"
    assert transition["transition_kind"] == "surface_state_changed"
    assert transition["logical_transition_proof"] == "trusted"
    assert transition["reason_code"] == "trusted_obstruction_clearance"


def test_offscreen_or_hidden_dialogs_do_not_keep_obstruction_chain_open() -> None:
    snapshot = _spa_snapshot(modal=False)
    baseline = _build(snapshot)
    snapshot["elements"].extend([
        _element(selector="#offscreen-dialog", tag="div", role="dialog",
                 rect={"x": -500, "y": 0, "width": 390, "height": 844}),
        {**_element(selector="#hidden-dialog", tag="div", role="dialog"),
         "visible": False, "style": {"display": "none"}},
    ])
    actual = _build(snapshot)
    assert actual["state_signals"]["obstruction_count"] == "0"
    assert actual["identity_fingerprint"] == baseline["identity_fingerprint"]


def test_same_url_spa_navigation_requires_two_independent_signal_groups() -> None:
    before = _build(_spa_snapshot(page="home"))
    after = _build(_spa_snapshot(page="profile"))

    transition = compare_h5_logical_surfaces(before=before, after=after)

    assert before["document_identity"] == after["document_identity"]
    assert before["identity_fingerprint"] != after["identity_fingerprint"]
    assert transition["transition_kind"] == "logical_page_changed"
    assert transition["logical_transition_proof"] == "trusted"
    assert set(transition["corroborating_signals"]) >= {
        "active_navigation",
        "stable_controls",
    }


def test_exact_dom_bottom_navigation_url_change_proves_main_structure_transition() -> None:
    """精确 DOM 底栏的同源 URL 变化可补足单一主结构变化的可信证明。"""
    before = _build(_spa_snapshot(), url="https://example.test/")
    after = deepcopy(before)
    after["identity_signals"]["main_structure"] = [{"category": "video_list"}]
    after["identity_fingerprint"] = stable_h5_surface_hash(after["identity_signals"])
    after["state_fingerprint"] = stable_h5_surface_hash(
        [after["identity_fingerprint"], after["state_signals"]]
    )
    proof = {
        "protocol_version": "bottom_navigation_transition_proof.v1",
        "status": "trusted",
        "effect": "same_origin_url_change",
        "before_location": "https://example.test/",
        "after_location": "https://example.test/home-video",
        "navigation_family": "footer_wrap",
        "navigation_index": 2,
        "before_identity_fingerprint": before["identity_fingerprint"],
        "after_identity_fingerprint": after["identity_fingerprint"],
    }

    without_proof = compare_h5_logical_surfaces(before=before, after=after)
    transition = compare_h5_logical_surfaces(
        before=before,
        after=after,
        bottom_navigation_proof=proof,
    )

    assert without_proof["transition_kind"] == "uncertain"
    assert without_proof["logical_transition_proof"] == "uncertain"
    assert transition["transition_kind"] == "logical_page_changed"
    assert transition["logical_transition_proof"] == "trusted"
    assert transition["reason_code"] == "trusted_bottom_navigation_url_change"
    assert transition["bottom_navigation_proof"] == {
        "protocol_version": "bottom_navigation_transition_proof.v1",
        "status": "trusted",
        "effect": "same_origin_url_change",
        "before_location_hash": stable_h5_surface_hash("https://example.test/"),
        "after_location_hash": stable_h5_surface_hash(
            "https://example.test/home-video"
        ),
        "origin_hash": stable_h5_surface_hash("https://example.test:443"),
        "navigation_family": "footer_wrap",
        "navigation_index": 2,
        "before_identity_fingerprint": before["identity_fingerprint"],
        "after_identity_fingerprint": after["identity_fingerprint"],
    }


@pytest.mark.parametrize("changed", [
    ("active_navigation", "page_heading"), ("active_navigation",), ("page_heading",),
])
def test_same_layout_bottom_navigation_requires_two_independent_identity_anchors(changed):
    before = _build(_spa_snapshot(), url="https://example.test/")
    after = deepcopy(before)
    for key in changed:
        after["identity_signals"][key] = [{"label": "另一个业务栏目"}]
    after["identity_fingerprint"] = stable_h5_surface_hash(after["identity_signals"])
    after["state_fingerprint"] = stable_h5_surface_hash([
        after["identity_fingerprint"], after["state_signals"],
    ])
    proof = {
        "protocol_version": "bottom_navigation_transition_proof.v1", "status": "trusted",
        "effect": "same_origin_url_change", "before_location": "https://example.test/",
        "after_location": "https://example.test/other", "navigation_family": "footer_wrap",
        "navigation_index": 2, "before_identity_fingerprint": before["identity_fingerprint"],
        "after_identity_fingerprint": after["identity_fingerprint"],
    }
    transition = compare_h5_logical_surfaces(
        before=before, after=after, bottom_navigation_proof=proof,
    )
    assert (transition["logical_transition_proof"] == "trusted") is (len(changed) == 2)
    assert (
        before["identity_signals"]["main_structure"]
        == after["identity_signals"]["main_structure"]
    )


def test_adaptive_bottom_navigation_proof_is_bound_to_pattern_signature() -> None:
    before = _build(_spa_snapshot(), url="https://example.test/")
    after = deepcopy(before)
    after["identity_signals"]["main_structure"] = [{"category": "short_video"}]
    after["identity_fingerprint"] = stable_h5_surface_hash(after["identity_signals"])
    after["state_fingerprint"] = stable_h5_surface_hash(
        [after["identity_fingerprint"], after["state_signals"]]
    )
    signature = "a" * 64
    proof = {
        "protocol_version": "bottom_navigation_transition_proof.v1",
        "status": "trusted",
        "effect": "same_origin_url_change",
        "before_location": "https://example.test/",
        "after_location": "https://example.test/short-video",
        "navigation_family": "adaptive_snapshot",
        "navigation_index": 2,
        "interaction_pattern_signature": signature,
        "before_identity_fingerprint": before["identity_fingerprint"],
        "after_identity_fingerprint": after["identity_fingerprint"],
    }

    transition = compare_h5_logical_surfaces(
        before=before,
        after=after,
        bottom_navigation_proof=proof,
    )

    assert transition["logical_transition_proof"] == "trusted"
    assert transition["bottom_navigation_proof"]["interaction_pattern_signature"] == signature

    invalid = compare_h5_logical_surfaces(
        before=before,
        after=after,
        bottom_navigation_proof={**proof, "interaction_pattern_signature": "b"},
    )
    assert invalid["reason_code"] == "bottom_navigation_transition_proof_invalid"


def test_adaptive_same_url_activation_change_proves_main_structure_transition() -> None:
    before = _build(_spa_snapshot(), url="https://example.test/")
    after = deepcopy(before)
    after["identity_signals"]["main_structure"] = [{"category": "short_video"}]
    after["identity_fingerprint"] = stable_h5_surface_hash(after["identity_signals"])
    after["state_fingerprint"] = stable_h5_surface_hash(
        [after["identity_fingerprint"], after["state_signals"]]
    )
    proof = {
        "protocol_version": "bottom_navigation_transition_proof.v1",
        "status": "trusted",
        "effect": "activation_state_change",
        "before_location": "https://example.test/",
        "after_location": "https://example.test/",
        "navigation_family": "adaptive_snapshot",
        "navigation_index": 2,
        "interaction_pattern_signature": "a" * 64,
        "activation_before_sha256": "sha256:" + "3" * 64,
        "activation_after_sha256": "sha256:" + "4" * 64,
        "before_identity_fingerprint": before["identity_fingerprint"],
        "after_identity_fingerprint": after["identity_fingerprint"],
    }

    transition = compare_h5_logical_surfaces(
        before=before,
        after=after,
        bottom_navigation_proof=proof,
    )

    assert transition["transition_kind"] == "logical_page_changed"
    assert transition["logical_transition_proof"] == "trusted"
    assert transition["reason_code"] == "trusted_bottom_navigation_activation_change"
    assert transition["bottom_navigation_proof"]["effect"] == "activation_state_change"
    assert transition["bottom_navigation_proof"]["before_location_hash"] == (
        transition["bottom_navigation_proof"]["after_location_hash"]
    )

    invalid = compare_h5_logical_surfaces(
        before=before,
        after=after,
        bottom_navigation_proof={
            **proof,
            "activation_after_sha256": proof["activation_before_sha256"],
        },
    )
    assert invalid["reason_code"] == "bottom_navigation_transition_proof_invalid"


def test_page_tab_activation_change_proves_main_structure_transition() -> None:
    """页内 Tab 的确定性选中态变化应证明同 URL 业务分支切换。"""
    before = _build(_spa_snapshot(), url="https://example.test/home")
    after = deepcopy(before)
    after["identity_signals"]["main_structure"] = [{"category": "latest_feed"}]
    after["identity_fingerprint"] = stable_h5_surface_hash(after["identity_signals"])
    after["state_fingerprint"] = stable_h5_surface_hash(
        [after["identity_fingerprint"], after["state_signals"]]
    )
    proof = {
        "protocol_version": "page_tab_transition_proof.v1",
        "status": "trusted",
        "effect": "activation_state_change",
        "before_location": "https://example.test/home",
        "after_location": "https://example.test/home",
        "target_selector_sha256": "sha256:" + "1" * 64,
        "target_label_sha256": "sha256:" + "2" * 64,
        "activation_before_sha256": "sha256:" + "3" * 64,
        "activation_after_sha256": "sha256:" + "4" * 64,
        "before_identity_fingerprint": before["identity_fingerprint"],
        "after_identity_fingerprint": after["identity_fingerprint"],
    }

    transition = compare_h5_logical_surfaces(
        before=before,
        after=after,
        page_tab_proof=proof,
    )

    assert transition["transition_kind"] == "logical_page_changed"
    assert transition["logical_transition_proof"] == "trusted"
    assert transition["reason_code"] == "trusted_page_tab_activation_change"
    assert transition["page_tab_proof"]["before_location_hash"] == (
        transition["page_tab_proof"]["after_location_hash"]
    )


def test_page_tab_activation_change_proves_same_page_filter_state() -> None:
    """同一业务页内排序筛选应留存为可信状态，不冒充新的逻辑页面。"""
    before = _build(_spa_snapshot(), url="https://example.test/home")
    after = deepcopy(before)
    after["state_signals"] = {
        **after["state_signals"],
        "viewport_state": "latest_uploads",
    }
    after["state_fingerprint"] = stable_h5_surface_hash(
        [after["identity_fingerprint"], after["state_signals"]]
    )
    proof = {
        "protocol_version": "page_tab_transition_proof.v1",
        "status": "trusted",
        "effect": "activation_state_change",
        "before_location": "https://example.test/home",
        "after_location": "https://example.test/home",
        "target_selector_sha256": "sha256:" + "1" * 64,
        "target_label_sha256": "sha256:" + "2" * 64,
        "activation_before_sha256": "sha256:" + "3" * 64,
        "activation_after_sha256": "sha256:" + "4" * 64,
        "before_identity_fingerprint": before["identity_fingerprint"],
        "after_identity_fingerprint": after["identity_fingerprint"],
    }

    transition = compare_h5_logical_surfaces(
        before=before, after=after, page_tab_proof=proof,
    )

    assert transition["transition_kind"] == "surface_state_changed"
    assert transition["logical_transition_proof"] == "trusted"
    assert transition["reason_code"] == "trusted_page_tab_state_change"
    assert "page_tab_activation" in transition["corroborating_signals"]


def test_page_tab_route_change_proves_new_logical_page_with_bound_document() -> None:
    """同源 URL 页签切换即使 DOM 结构相同，也不是跑马灯式局部变化。"""
    before = _build(_spa_snapshot(), url="https://example.test/list?type=1")
    after = deepcopy(before)
    proof = {
        "protocol_version": "page_tab_transition_proof.v1",
        "status": "trusted",
        "effect": "route_activation_change",
        "before_location": "https://example.test/list?type=1",
        "after_location": "https://example.test/list?type=2",
        "target_selector_sha256": "sha256:" + "1" * 64,
        "target_label_sha256": "sha256:" + "2" * 64,
        "activation_before_sha256": "sha256:" + "3" * 64,
        "activation_after_sha256": "sha256:" + "4" * 64,
        "before_identity_fingerprint": before["identity_fingerprint"],
        "after_identity_fingerprint": after["identity_fingerprint"],
    }

    transition = compare_h5_logical_surfaces(
        before=before,
        after=after,
        page_tab_proof=proof,
    )

    assert transition["transition_kind"] == "logical_page_changed", transition
    assert transition["logical_transition_proof"] == "trusted"
    assert transition["reason_code"] == "trusted_page_tab_route_change"
    assert transition["page_tab_proof"]["before_location_hash"] != (
        transition["page_tab_proof"]["after_location_hash"]
    )

    for unsafe_after in (
        {**after, "evidence_quality": "insufficient"},
        {**after, "document_identity": "sha256:" + "9" * 64},
    ):
        rejected = compare_h5_logical_surfaces(
            before=before, after=unsafe_after, page_tab_proof=proof,
        )
        assert rejected["logical_transition_proof"] == "insufficient"
        assert rejected["reason_code"] == "page_tab_route_change_unbound"

    missing_document = compare_h5_logical_surfaces(
        before={**before, "document_identity": None},
        after={**after, "document_identity": None},
        page_tab_proof=proof,
    )
    assert missing_document["logical_transition_proof"] == "insufficient"
    assert missing_document["reason_code"] == "page_tab_route_change_unbound"

    rejected_origin = compare_h5_logical_surfaces(
        before=before,
        after=after,
        page_tab_proof={**proof, "after_location": "https://other.test/list?type=2"},
    )
    assert rejected_origin["reason_code"] == "page_tab_transition_proof_invalid"


def test_page_tab_content_panel_change_proves_same_normalized_state() -> None:
    """列表内容被状态指纹降噪时，面板替换摘要仍应证明同页筛选成功。"""
    before = _build(_spa_snapshot(), url="https://example.test/home")
    after = deepcopy(before)
    proof = {
        "protocol_version": "page_tab_transition_proof.v1",
        "status": "trusted",
        "effect": "activation_state_change",
        "before_location": "https://example.test/home",
        "after_location": "https://example.test/home",
        "target_selector_sha256": "sha256:" + "1" * 64,
        "target_label_sha256": "sha256:" + "2" * 64,
        "activation_before_sha256": "sha256:" + "3" * 64,
        "activation_after_sha256": "sha256:" + "4" * 64,
        "before_identity_fingerprint": before["identity_fingerprint"],
        "after_identity_fingerprint": after["identity_fingerprint"],
        "content_before_sha256": "sha256:" + "5" * 64,
        "content_after_sha256": "sha256:" + "6" * 64,
        "content_change_count": 2,
    }

    transition = compare_h5_logical_surfaces(
        before=before,
        after=after,
        page_tab_proof=proof,
    )

    assert transition["transition_kind"] == "surface_state_changed"
    assert transition["logical_transition_proof"] == "trusted"
    assert transition["reason_code"] == "trusted_page_tab_state_change"
    assert transition["page_tab_proof"]["content_change_count"] == 2


@pytest.mark.parametrize(
    "proof_override",
    [
        pytest.param(
            {"after_location": "https://example.test/other"},
            id="url-changed",
        ),
        pytest.param(
            {"activation_after_sha256": "sha256:" + "3" * 64},
            id="activation-unchanged",
        ),
        pytest.param(
            {"after_identity_fingerprint": "sha256:" + "9" * 64},
            id="identity-unbound",
        ),
    ],
)
def test_page_tab_transition_proof_fails_closed(
    proof_override: dict[str, str],
) -> None:
    before = _build(_spa_snapshot(), url="https://example.test/home")
    after = deepcopy(before)
    after["identity_signals"]["main_structure"] = [{"category": "latest_feed"}]
    after["identity_fingerprint"] = stable_h5_surface_hash(after["identity_signals"])
    after["state_fingerprint"] = stable_h5_surface_hash(
        [after["identity_fingerprint"], after["state_signals"]]
    )
    proof = {
        "protocol_version": "page_tab_transition_proof.v1",
        "status": "trusted",
        "effect": "activation_state_change",
        "before_location": "https://example.test/home",
        "after_location": "https://example.test/home",
        "target_selector_sha256": "sha256:" + "1" * 64,
        "target_label_sha256": "sha256:" + "2" * 64,
        "activation_before_sha256": "sha256:" + "3" * 64,
        "activation_after_sha256": "sha256:" + "4" * 64,
        "before_identity_fingerprint": before["identity_fingerprint"],
        "after_identity_fingerprint": after["identity_fingerprint"],
        **proof_override,
    }

    transition = compare_h5_logical_surfaces(
        before=before,
        after=after,
        page_tab_proof=proof,
    )

    assert transition["transition_kind"] == "uncertain"
    assert transition["logical_transition_proof"] == "insufficient"
    assert transition["reason_code"] == "page_tab_transition_proof_invalid"


def test_pages_foot_sparse_dom_navigation_proves_ai_to_profile_transition() -> None:
    """阻止自定义 li 底栏被压成空身份并误拒绝真实 AI→我的跳转。"""
    before = _build(
        _pages_foot_sparse_snapshot(active_index=5),
        url="https://example.test/ai/generate",
    )
    after = _build(
        _pages_foot_sparse_snapshot(active_index=6),
        url="https://example.test/mine",
    )
    proof = {
        "protocol_version": "bottom_navigation_transition_proof.v1",
        "status": "trusted",
        "effect": "same_origin_url_change",
        "before_location": "https://example.test/ai/generate",
        "after_location": "https://example.test/mine",
        "navigation_family": "pages_foot",
        "navigation_index": 6,
        "before_identity_fingerprint": before["identity_fingerprint"],
        "after_identity_fingerprint": after["identity_fingerprint"],
    }

    transition = compare_h5_logical_surfaces(
        before=before,
        after=after,
        bottom_navigation_proof=proof,
    )

    assert before["identity_fingerprint"] != after["identity_fingerprint"]
    assert before["identity_signals"]["active_navigation"][0]["label"] == "ai"
    assert after["identity_signals"]["active_navigation"][0]["label"] == "我的"
    assert "main_structure" in transition["corroborating_signals"]
    assert transition["transition_kind"] == "logical_page_changed"
    assert transition["logical_transition_proof"] == "trusted"
    assert transition["reason_code"] == "trusted_bottom_navigation_url_change"


@pytest.mark.parametrize(
    ("proof_override", "reason_code"),
    [
        pytest.param(
            {"after_location": "https://other.test/home-video"},
            "bottom_navigation_transition_proof_invalid",
            id="cross-origin",
        ),
        pytest.param(
            {"after_identity_fingerprint": "sha256:" + "9" * 64},
            "bottom_navigation_transition_proof_invalid",
            id="identity-unbound",
        ),
    ],
)
def test_dom_bottom_navigation_proof_fails_closed(
    proof_override: dict[str, str],
    reason_code: str,
) -> None:
    before = _build(_spa_snapshot(), url="https://example.test/")
    after = deepcopy(before)
    after["identity_signals"]["main_structure"] = [{"category": "video_list"}]
    after["identity_fingerprint"] = stable_h5_surface_hash(after["identity_signals"])
    after["state_fingerprint"] = stable_h5_surface_hash(
        [after["identity_fingerprint"], after["state_signals"]]
    )
    proof = {
        "protocol_version": "bottom_navigation_transition_proof.v1",
        "status": "trusted",
        "effect": "same_origin_url_change",
        "before_location": "https://example.test/",
        "after_location": "https://example.test/home-video",
        "navigation_family": "footer_wrap",
        "navigation_index": 2,
        "before_identity_fingerprint": before["identity_fingerprint"],
        "after_identity_fingerprint": after["identity_fingerprint"],
        **proof_override,
    }

    transition = compare_h5_logical_surfaces(
        before=before,
        after=after,
        bottom_navigation_proof=proof,
    )

    assert transition["transition_kind"] == "uncertain"
    assert transition["logical_transition_proof"] == "insufficient"
    assert transition["reason_code"] == reason_code


def test_hash_change_without_structure_change_does_not_create_page() -> None:
    before = _build(_spa_snapshot(), url="https://example.test/#home")
    after = _build(_spa_snapshot(), url="https://example.test/#profile")

    transition = compare_h5_logical_surfaces(before=before, after=after)

    assert before["identity_fingerprint"] == after["identity_fingerprint"]
    assert transition["transition_kind"] == "unchanged"


def test_document_identity_does_not_include_url_origin() -> None:
    first = _build(_spa_snapshot(), url="https://first.example.test/home")
    second = _build(_spa_snapshot(), url="https://second.example.test/home")

    assert first["document_identity"] == second["document_identity"]


def test_dynamic_list_content_and_document_title_do_not_change_identity() -> None:
    before_snapshot = _spa_snapshot()
    before_snapshot["title"] = "用户 A 的推荐内容"
    before_snapshot["elements"].append(
        _element(
            selector="#feed-card-101 a",
            tag="a",
            text="用户 A 发布的动态标题",
            role="link",
            class_tokens=["feed-card", "item-title"],
            interactive=True,
        )
    )
    after_snapshot = _spa_snapshot()
    after_snapshot["title"] = "用户 B 的推荐内容"
    after_snapshot["elements"].append(
        _element(
            selector="#feed-card-902 a",
            tag="a",
            text="完全不同的用户动态正文",
            role="link",
            class_tokens=["feed-card", "item-title"],
            interactive=True,
        )
    )

    before = _build(before_snapshot)
    after = _build(after_snapshot)

    assert before["identity_fingerprint"] == after["identity_fingerprint"]


def test_dynamic_announcement_content_does_not_create_a_new_page_identity() -> None:
    """公告、轮播等动态展示内容只能改变页面状态，不能制造业务页面。"""

    before_snapshot = _spa_snapshot()
    before_snapshot["elements"].append(
        _element(
            selector=".notice-swiper h3",
            tag="h3",
            text="系统公告：今日活动一",
            class_tokens=["notice-title"],
            ancestor_class_tokens=["notice-swiper", "announcement-banner"],
        )
    )
    after_snapshot = _spa_snapshot()
    after_snapshot["elements"].append(
        _element(
            selector=".notice-swiper h3",
            tag="h3",
            text="系统公告：完全不同的活动二",
            class_tokens=["notice-title"],
            ancestor_class_tokens=["notice-swiper", "announcement-banner"],
        )
    )

    before = _build(before_snapshot)
    after = _build(after_snapshot)

    assert before["identity_fingerprint"] == after["identity_fingerprint"]


def test_pure_flutter_canvas_without_semantics_is_interactively_blocked() -> None:
    snapshot = {
        "schema_version": "ui-snapshot.v2",
        "url": URL,
        "title": "Flutter Canvas",
        "flutter_semantics_activation": "failed",
        "document": {"flutter_root_present": True},
        "elements": [
            _element(
                selector="canvas",
                tag="canvas",
                class_tokens=["flutter-canvas"],
            )
        ],
    }

    surface = _build(snapshot)

    assert surface["render_profile"]["kind"] == "flutter_web"
    assert surface["render_profile"]["status"] == "confirmed"
    assert "flutter_semantics_unavailable" in surface["degraded_reasons"]
    assert "interactive_exploration_blocked" in surface["degraded_reasons"]


def test_render_profile_confidence_never_raises_page_type_confidence() -> None:
    surface = _build(_flutter_snapshot())

    bound = bind_h5_page_type(
        logical_surface=surface,
        page_type="profile",
        detection_status="recognized",
        page_type_confidence=0.42,
        confidence_source="model_self_report",
    )

    assert bound is not None
    assert bound["confidence_domains"]["render_profile_confidence"] >= 0.9
    assert bound["confidence_domains"]["page_type_confidence"] == 0.42
    assert bound["page_type_binding"]["coverage_allowed"] is False
    assert bound["page_type_binding"]["reason_code"] == "page_type_low_confidence"


def test_confirmed_flutter_still_requires_trusted_transition_for_coverage() -> None:
    surface = _build(_flutter_snapshot())

    bound = bind_h5_page_type(
        logical_surface=surface,
        page_type="profile",
        detection_status="recognized",
        page_type_confidence=0.91,
        confidence_source="model_self_report",
    )

    assert bound is not None
    assert bound["page_type_binding"]["identity_fingerprint"] == (
        bound["identity_fingerprint"]
    )
    assert bound["page_type_binding"]["coverage_allowed"] is False
    assert bound["page_type_binding"]["reason_code"] == "logical_transition_untrusted"


def test_dom_page_without_spa_evidence_remains_unclassified_and_compatible() -> None:
    snapshot = _spa_snapshot()
    snapshot["document"] = {"navigation_type": "navigate"}

    surface = _build(snapshot)
    bound = bind_h5_page_type(
        logical_surface=surface,
        page_type="home",
        detection_status="recognized",
        page_type_confidence=0.91,
        confidence_source="model_self_report",
    )

    assert surface["render_profile"] == {
        "kind": "dom_unclassified",
        "status": "observed",
        "confidence": 0.5,
        "signals": ["dom_elements_present"],
    }
    assert bound is not None
    assert bound["page_type_binding"]["coverage_allowed"] is True
