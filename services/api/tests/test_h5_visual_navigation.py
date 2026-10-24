"""H5 Flutter 视觉坐标导航的领域合同测试。"""

from __future__ import annotations

from typing import Any

import pytest

from services.api.app.domain.exploration.h5_logical_surface import (
    stable_h5_surface_hash,
)
from services.api.app.domain.exploration.h5_visual_navigation import (
    VisualNavigationContractError,
    build_visual_navigation_capability,
    classify_flutter_semantics_coverage,
    evaluate_visual_navigation_target,
    normalize_flutter_rect_to_visual_viewport,
    public_visual_navigation_audit,
    verify_visual_navigation_capability,
    visual_navigation_region_digests,
)


def _valid_capability_values(**updates: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "source_task_id": "task-1",
        "source_capture_id": "capture-1",
        "source_artifact_id": "artifact-1",
        "source_artifact_sha256": "sha256:" + "a" * 64,
        "source_identity_fingerprint": "sha256:" + "b" * 64,
        "source_state_fingerprint": "sha256:" + "c" * 64,
        "source_page_type": "profile",
        "source_page_type_confidence": 0.92,
        "source_identity_bound": True,
        "render_profile": {"kind": "flutter_web", "status": "confirmed"},
        "semantics_coverage_level": "semantics_sparse",
        "target_kind": "profile_payment",
        "label": "会员中心",
        "expected_page_type": "payment",
        "visual_target_confidence": 0.93,
        "selected": False,
        "unique": True,
        "risk_flags": (),
        "normalized_rect": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.1},
        "source_image_size": {"width": 390, "height": 844},
        "viewport_metrics": {
            "width": 390,
            "height": 844,
            "offset_left": 0,
            "offset_top": 0,
            "page_left": 0,
            "page_top": 0,
            "scale": 1,
        },
        "model_image_transform": {
            "kind": "identity",
            "scale_x": 1.0,
            "scale_y": 1.0,
            "offset_x": 0.0,
            "offset_y": 0.0,
        },
        "local_visual_digest": "sha256:" + "d" * 64,
        "surrounding_anchor_digest": "sha256:" + "e" * 64,
    }
    values.update(updates)
    return values


@pytest.mark.parametrize("host_scale", [1.0, 0.5])
def test_flutter_rect_policy_uses_viewport_css_pixels(host_scale: float) -> None:
    """Flutter 局部矩形必须先应用 host 缩放，才能参与视口绝对阈值。"""
    rect = normalize_flutter_rect_to_visual_viewport(
        local_rect={"left": 420, "top": 100, "width": 120, "height": 80},
        host_transform=(host_scale, 0.0, 0.0, host_scale, 0.0, 0.0),
        viewport_offset=(0.0, 0.0),
    )

    assert rect.width == 120.0 * host_scale
    assert rect.coordinate_space == "visual_viewport_css_pixels"


def test_flutter_rect_policy_applies_full_affine_transform_and_viewport_offset() -> None:
    """旋转/斜切/平移分量和视觉视口偏移均不得在归一化时丢失。"""
    rect = normalize_flutter_rect_to_visual_viewport(
        local_rect={"left": 10, "top": 20, "width": 30, "height": 40},
        host_transform=(1.0, 0.5, -0.25, 2.0, 100.0, 50.0),
        viewport_offset=(5.0, 7.0),
    )

    assert rect.left == 90.0
    assert rect.top == 88.0
    assert rect.width == 40.0
    assert rect.height == 95.0


def _flutter_elements(*, usable: int, text: str) -> list[dict[str, Any]]:
    return [
        {
            "tag": "flt-semantics",
            "selector": f"flt-semantics#flt-semantic-node-{index}",
            "text": text,
            "role": "button",
            "interactive": True,
            "visible": True,
            "hit_test": {"center_hits_target": True},
        }
        for index in range(usable)
    ]


def test_semantics_sparse_when_text_is_poor_despite_activation() -> None:
    """节点少于阈值时不得把已激活 Semantics 当作充分证据。"""
    result = classify_flutter_semantics_coverage(
        elements=_flutter_elements(usable=9, text="首页视频排行帖子我的设置签到活动支付会员中心"),
        ocr_text="首页 视频 排行 帖子 我的 设置 签到 会员中心",
    )

    assert result["level"] == "semantics_sparse"
    assert result["usable_node_count"] == 9
    assert result["degraded_reasons"] == ["usable_node_count_below_threshold"]


def test_semantics_sufficient_requires_ocr_ratio_when_ocr_is_large() -> None:
    """OCR 足够长时，覆盖档位必须同时满足跨证据文本比例。"""
    result = classify_flutter_semantics_coverage(
        elements=_flutter_elements(usable=14, text="首页视频排行帖子我的设置签到活动支付"),
        ocr_text="首页视频排行帖子我的设置签到活动支付内容详情推荐专题热门最新"
        "首页短视频排行榜社区帖子会员活动金币签到系统设置账户支付中心",
    )

    assert result["level"] == "semantics_sufficient"
    assert result["semantic_ocr_text_coverage"] >= 0.35


def test_semantics_missing_ocr_uses_node_and_text_thresholds_only() -> None:
    """缺少 OCR 时，达标的节点和语义文本应足以给出充分档位。"""
    result = classify_flutter_semantics_coverage(
        elements=_flutter_elements(usable=14, text="首页视频排行帖子我的设置签到活动支付会员中心"),
        ocr_text=None,
    )

    assert result["level"] == "semantics_sufficient"
    assert result["degraded_reasons"] == []


def test_semantics_short_ocr_uses_node_and_text_thresholds_only() -> None:
    """短 OCR 不得降低已由 Semantics 节点和文本证明的充分档位。"""
    result = classify_flutter_semantics_coverage(
        elements=_flutter_elements(usable=14, text="首页视频排行帖子我的设置签到活动支付会员中心"),
        ocr_text="首页 视频 排行",
    )

    assert result["level"] == "semantics_sufficient"
    assert result["degraded_reasons"] == []


def test_semantics_counts_only_explicitly_visible_stable_nodes() -> None:
    """缺可见性、空角色、正文重复及动态 ID 节点均不能抬高覆盖节点数。"""
    result = classify_flutter_semantics_coverage(
        elements=[
            {
                "tag": "flt-semantics",
                "role": "button",
                "text": "首页",
                "interactive": True,
                "visible": True,
                "hit_test": {"center_hits_target": True},
            },
            {
                "tag": "flt-semantics",
                "role": "button",
                "text": "被无关遮罩覆盖",
                "interactive": True,
                "visible": True,
                "hit_test": {"center_hits_target": False},
            },
            {
                "tag": "flt-semantics",
                "role": "button",
                "text": "缺少命中事实",
                "interactive": True,
                "visible": True,
            },
            {
                "tag": "flt-semantics",
                "role": "button",
                "text": "缺少可见性",
                "interactive": True,
            },
            {
                "tag": "flt-semantics",
                "role": "button",
                "text": "不可见",
                "interactive": True,
                "visible": False,
            },
            {
                "tag": "flt-semantics",
                "role": "button",
                "text": "零可见性",
                "interactive": True,
                "visible": 0,
            },
            {
                "tag": "flt-semantics",
                "role": "",
                "text": "空角色",
                "interactive": True,
                "visible": True,
            },
            {
                "tag": "flt-semantics",
                "role": "text",
                "text": "重复正文",
                "interactive": False,
                "visible": True,
                "class_tokens": ["feed"],
            },
            {
                "tag": "flt-semantics",
                "selector": "flt-semantics#flt-semantic-node-901",
                "role": "",
                "text": "",
                "interactive": False,
                "visible": True,
            },
        ],
        ocr_text=None,
    )

    assert result["usable_node_count"] == 1


@pytest.mark.parametrize(
    ("target_kind", "expected_page_type"),
    [
        ("profile_settings", "settings"),
        ("profile_payment", "payment"),
        ("profile_activity", "activity"),
    ],
)
def test_profile_local_targets_are_rejected_outside_profile(
    target_kind: str,
    expected_page_type: str,
) -> None:
    """个人中心局部目标不得从其他页面签发。"""
    decision = evaluate_visual_navigation_target(
        target_kind=target_kind,
        source_page_type="home",
        expected_page_type=expected_page_type,
        visual_target_confidence=0.99,
        selected=False,
        risk_flags=(),
    )

    assert decision.allowed is False
    assert decision.reason_code == "visual_profile_source_required"


@pytest.mark.parametrize(
    ("target_kind", "confidence"),
    [("primary_navigation", 0.84), ("content_card", 0.89)],
)
def test_visual_navigation_rejects_confidence_below_kind_floor(
    target_kind: str,
    confidence: float,
) -> None:
    """各类视觉目标低于固定下限时不得因四舍五入或模型高分措辞获签。"""
    expected_page_type = "profile" if target_kind == "primary_navigation" else "content_detail"
    decision = evaluate_visual_navigation_target(
        target_kind=target_kind,
        source_page_type="home",
        expected_page_type=expected_page_type,
        visual_target_confidence=confidence,
        selected=False,
        risk_flags=(),
        normalized_rect={"x": 0.1, "y": 0.88, "width": 0.2, "height": 0.1},
    )

    assert decision.allowed is False
    assert decision.reason_code == "visual_target_confidence_low"


def test_selected_primary_navigation_is_rejected() -> None:
    """已选中的底栏项不是导航机会，不能再次签发点击能力。"""
    decision = evaluate_visual_navigation_target(
        target_kind="primary_navigation",
        source_page_type="home",
        expected_page_type="profile",
        visual_target_confidence=0.95,
        selected=True,
        risk_flags=(),
        normalized_rect={"x": 0.75, "y": 0.88, "width": 0.2, "height": 0.1},
    )

    assert decision.allowed is False
    assert decision.reason_code == "visual_target_already_selected"


@pytest.mark.parametrize(
    "risk_value",
    [
        "download",
        "external",
        "ad",
        "submit",
        "login_submit",
        "充值",
        "确认支付",
    ],
)
def test_visual_navigation_rejects_unsafe_risk_wording(risk_value: str) -> None:
    """下载、外链、广告、提交和交易执行措辞不能伪装成低风险导航。"""
    decision = evaluate_visual_navigation_target(
        target_kind="content_card",
        source_page_type="home",
        expected_page_type="content_detail",
        visual_target_confidence=0.95,
        selected=False,
        risk_flags=(risk_value,),
        normalized_rect={"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.3},
    )

    assert decision.allowed is False
    assert decision.reason_code == "visual_target_risk_flagged"


def test_visual_navigation_rejects_advertisement_label_without_model_risk_flag() -> None:
    """模型漏报 risk_flags 时，广告标签本身仍必须触发失败关闭。"""
    decision = evaluate_visual_navigation_target(
        target_kind="content_card",
        source_page_type="home",
        expected_page_type="content_detail",
        visual_target_confidence=0.99,
        selected=False,
        risk_flags=(),
        label="广告",
        normalized_rect={"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.3},
    )

    assert decision.allowed is False
    assert decision.reason_code == "visual_target_risk_flagged"


@pytest.mark.parametrize(
    ("target_kind", "expected_page_type"),
    [
        ("profile_settings", "settings"),
        ("profile_payment", "payment"),
        ("profile_activity", "activity"),
    ],
)
def test_every_profile_local_target_requires_confident_identity_binding(
    target_kind: str,
    expected_page_type: str,
) -> None:
    """个人中心局部入口必须同时绑定已识别且至少 0.7 置信度的 profile 身份。"""
    low_confidence = evaluate_visual_navigation_target(
        target_kind=target_kind,
        source_page_type="profile",
        source_page_type_confidence=0.69,
        source_identity_bound=True,
        expected_page_type=expected_page_type,
        visual_target_confidence=0.95,
        selected=False,
        risk_flags=(),
        normalized_rect={"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.2},
    )
    unbound = evaluate_visual_navigation_target(
        target_kind=target_kind,
        source_page_type="profile",
        source_page_type_confidence=0.95,
        source_identity_bound=False,
        expected_page_type=expected_page_type,
        visual_target_confidence=0.95,
        selected=False,
        risk_flags=(),
        normalized_rect={"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.2},
    )

    assert low_confidence.reason_code == "visual_profile_source_confidence_low"
    assert unbound.reason_code == "visual_profile_identity_required"


def test_visual_navigation_rejects_non_unique_and_full_page_targets() -> None:
    """重复候选和全页巨框必须在签发前失败关闭。"""
    duplicate = evaluate_visual_navigation_target(
        target_kind="video_card",
        source_page_type="video_list",
        expected_page_type="video_detail",
        visual_target_confidence=0.95,
        selected=False,
        risk_flags=(),
        unique=False,
        normalized_rect={"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.3},
    )
    giant = evaluate_visual_navigation_target(
        target_kind="content_card",
        source_page_type="home",
        expected_page_type="content_detail",
        visual_target_confidence=0.95,
        selected=False,
        risk_flags=(),
        normalized_rect={"x": 0.01, "y": 0.01, "width": 0.98, "height": 0.98},
    )

    assert duplicate.reason_code == "visual_target_not_unique"
    assert giant.reason_code == "visual_target_full_page_rect"


def test_primary_navigation_must_be_in_bottom_band() -> None:
    """主导航框离开底部带时不能只凭别名命中获签。"""
    decision = evaluate_visual_navigation_target(
        target_kind="primary_navigation",
        source_page_type="home",
        expected_page_type="profile",
        visual_target_confidence=0.95,
        selected=False,
        risk_flags=(),
        normalized_rect={"x": 0.75, "y": 0.2, "width": 0.2, "height": 0.1},
    )

    assert decision.allowed is False
    assert decision.reason_code == "visual_primary_navigation_not_bottom_band"


def test_profile_payment_is_only_a_medium_risk_page_entry_intent() -> None:
    """支付类视觉能力只能表达进入 payment 页，不能表达充值或确认支付。"""
    decision = evaluate_visual_navigation_target(
        target_kind="profile_payment",
        source_page_type="profile",
        source_page_type_confidence=0.95,
        source_identity_bound=True,
        expected_page_type="payment",
        visual_target_confidence=0.95,
        selected=False,
        risk_flags=(),
        normalized_rect={"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.15},
    )

    assert decision.allowed is True
    assert decision.risk_level == "medium"
    assert decision.semantic_target == "进入 payment 页面"
    assert "submit" not in decision.target_keywords
    assert "确认支付" not in decision.target_keywords


def test_visual_navigation_capability_round_trips_and_public_audit_omits_digests() -> None:
    """可执行能力须经严格复核，公开审计不能泄露图像局部摘要。"""
    capability = build_visual_navigation_capability(**_valid_capability_values())

    verified = verify_visual_navigation_capability(capability)
    audit = public_visual_navigation_audit(capability)

    assert verified.target_kind == "profile_payment"
    assert verified.normalized_rect["width"] == 0.3
    assert audit["target_kind"] == "profile_payment"
    assert capability["protocol_version"] == "visual_navigation_coordinate.v1"
    assert capability["normalized_label_digest"].startswith("sha256:")
    assert capability["target_signature"].startswith("sha256:")
    assert "label" not in capability
    assert "local_visual_digest" not in audit
    assert "surrounding_anchor_digest" not in audit
    assert "target_signature" not in audit


def test_visual_navigation_capability_rejects_signed_field_tampering() -> None:
    """任一来源或策略字段被持久化链路改写后，验签必须失败。"""
    capability = build_visual_navigation_capability(**_valid_capability_values())

    with pytest.raises(ValueError, match="签名"):
        verify_visual_navigation_capability(
            {**capability, "expected_page_type": "settings"}
        )


def test_visual_navigation_capability_binds_source_task() -> None:
    """能力不能从一个任务复制到另一个任务继续使用。"""
    capability = build_visual_navigation_capability(**_valid_capability_values())

    assert capability["source_task_id"] == "task-1"
    with pytest.raises(ValueError, match="签名"):
        verify_visual_navigation_capability(
            {**capability, "source_task_id": "task-2"}
        )


def test_visual_navigation_capability_rejects_rehashed_noncanonical_payload() -> None:
    """调用方重签非 canonical 精度也不能制造可通过复验的能力。"""
    capability = build_visual_navigation_capability(**_valid_capability_values())
    tampered = {
        **capability,
        "visual_target_confidence": 0.93000001,
    }
    tampered["target_signature"] = stable_h5_surface_hash(
        {
            key: value
            for key, value in tampered.items()
            if key != "target_signature"
        }
    )

    with pytest.raises(ValueError, match="签名"):
        verify_visual_navigation_capability(tampered)


def test_visual_navigation_capability_uses_canonical_label_digest() -> None:
    """全角空白和展示间距不能为同一 OCR 标签制造不同密封身份。"""
    compact = build_visual_navigation_capability(**_valid_capability_values())
    spaced = build_visual_navigation_capability(
        **_valid_capability_values(label=" 会员　中心 ")
    )

    assert spaced["normalized_label_digest"] == compact["normalized_label_digest"]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("visual_target_confidence", 1.00001),
        ("visual_target_confidence", -0.00001),
        ("visual_target_confidence", float("nan")),
        ("normalized_rect.x", 1.0000001),
        ("normalized_rect.x", -0.0000001),
        ("normalized_rect.x", float("nan")),
        ("normalized_rect.width", 0.9000001),
    ],
)
def test_capability_checks_raw_unit_bounds_before_rounding(
    field_name: str,
    value: float,
) -> None:
    """轻微越界和 NaN 不能经四舍五入后伪装成合法置信度或坐标。"""
    values = _valid_capability_values()
    if field_name == "visual_target_confidence":
        values[field_name] = value
    else:
        rect_field = field_name.rsplit(".", 1)[-1]
        values["normalized_rect"] = {
            **values["normalized_rect"],
            rect_field: value,
        }

    with pytest.raises(ValueError, match="有限数值|0 到 1|视觉视口"):
        build_visual_navigation_capability(**values)


def test_capability_rejects_out_of_bounds_normalized_coordinate() -> None:
    """坐标超出视觉视口必须在领域边界失败，避免执行器接收非法目标。"""
    with pytest.raises(ValueError, match="normalized_rect"):
        build_visual_navigation_capability(
            **_valid_capability_values(
                normalized_rect={"x": 0.8, "y": 0.2, "width": 0.3, "height": 0.1}
            )
        )


@pytest.mark.parametrize(
    ("target_kind", "source_page_type", "expected_page_type", "rect"),
    [
        (
            "primary_navigation",
            "home",
            "profile",
            {"x": 0.0, "y": 0.88, "width": 1.0, "height": 0.12},
        ),
        (
            "content_card",
            "home",
            "content_detail",
            {"x": 0.01, "y": 0.10, "width": 0.98, "height": 0.60},
        ),
        (
            "video_card",
            "home",
            "video_detail",
            {"x": 0.05, "y": 0.05, "width": 0.9, "height": 0.7},
        ),
        (
            "profile_payment",
            "profile",
            "payment",
            {"x": 0.02, "y": 0.1, "width": 0.96, "height": 0.36},
        ),
    ],
)
def test_visual_target_geometry_rejects_oversized_kind_specific_rectangles(
    target_kind: str,
    source_page_type: str,
    expected_page_type: str,
    rect: dict[str, float],
) -> None:
    """单槽底栏和局部入口不能用覆盖主体的大框伪造可点击区域。"""
    decision = evaluate_visual_navigation_target(
        target_kind=target_kind,
        source_page_type=source_page_type,
        expected_page_type=expected_page_type,
        visual_target_confidence=0.99,
        selected=False,
        risk_flags=(),
        source_page_type_confidence=0.95,
        source_identity_bound=True,
        normalized_rect=rect,
    )

    assert decision.allowed is False
    assert decision.reason_code == "visual_target_geometry_invalid"


@pytest.mark.parametrize(
    ("target_kind", "source_page_type", "expected_page_type", "rect"),
    [
        (
            "primary_navigation",
            "home",
            "profile",
            {"x": 0.78, "y": 0.9, "width": 0.18, "height": 0.08},
        ),
        (
            "content_card",
            "home",
            "content_detail",
            {"x": 0.08, "y": 0.2, "width": 0.84, "height": 0.3},
        ),
        (
            "video_card",
            "home",
            "video_detail",
            {"x": 0.06, "y": 0.12, "width": 0.88, "height": 0.36},
        ),
        (
            "profile_settings",
            "profile",
            "settings",
            {"x": 0.88, "y": 0.04, "width": 0.08, "height": 0.06},
        ),
        (
            "profile_activity",
            "profile",
            "activity",
            {"x": 0.06, "y": 0.3, "width": 0.88, "height": 0.1},
        ),
        (
            "content_card",
            "home",
            "content_detail",
            {"x": 0.18, "y": 0.6, "width": 0.8, "height": 0.4},
        ),
        (
            "profile_payment",
            "profile",
            "payment",
            {"x": 0.18, "y": 0.79, "width": 0.8, "height": 0.15},
        ),
    ],
)
def test_visual_target_geometry_keeps_reasonable_kind_specific_rectangles(
    target_kind: str,
    source_page_type: str,
    expected_page_type: str,
    rect: dict[str, float],
) -> None:
    """合理的底栏单槽、内容卡和 Profile 图标仍可通过策略。"""
    decision = evaluate_visual_navigation_target(
        target_kind=target_kind,
        source_page_type=source_page_type,
        expected_page_type=expected_page_type,
        visual_target_confidence=0.99,
        selected=False,
        risk_flags=(),
        source_page_type_confidence=0.95,
        source_identity_bound=True,
        normalized_rect=rect,
    )

    assert decision.allowed is True


@pytest.mark.parametrize(
    ("target_kind", "source_page_type", "expected_page_type", "rect"),
    [
        (
            "content_card",
            "home",
            "content_detail",
            {"x": 0.01, "y": 0.1, "width": 0.8, "height": 0.3},
        ),
        (
            "video_card",
            "home",
            "video_detail",
            {"x": 0.1, "y": 0.1, "width": 0.72, "height": 0.1},
        ),
        (
            "content_card",
            "home",
            "content_detail",
            {"x": 0.1, "y": 0.1, "width": 0.16, "height": 0.4},
        ),
        (
            "profile_settings",
            "profile",
            "settings",
            {"x": 0.1, "y": 0.1, "width": 0.84, "height": 0.05},
        ),
        (
            "profile_activity",
            "profile",
            "activity",
            {"x": 0.1, "y": 0.89, "width": 0.8, "height": 0.1},
        ),
    ],
)
def test_visual_target_geometry_rejects_position_or_aspect_escape(
    target_kind: str,
    source_page_type: str,
    expected_page_type: str,
    rect: dict[str, float],
) -> None:
    """局部目标不能越过 kind 专属位置边界或用极端长宽比伪造。"""
    decision = evaluate_visual_navigation_target(
        target_kind=target_kind,
        source_page_type=source_page_type,
        expected_page_type=expected_page_type,
        visual_target_confidence=0.99,
        selected=False,
        risk_flags=(),
        source_page_type_confidence=0.95,
        source_identity_bound=True,
        normalized_rect=rect,
    )

    assert decision.allowed is False
    assert decision.reason_code == "visual_target_geometry_invalid"


@pytest.mark.parametrize(
    (
        "target_kind",
        "source_page_type",
        "expected_page_type",
        "boundary_rect",
        "excess_rect",
    ),
    [
        (
            "primary_navigation",
            "home",
            "profile",
            {"x": 0.0, "y": 0.8, "width": 0.4, "height": 0.2},
            {"x": 0.0, "y": 0.8, "width": 0.400001, "height": 0.1},
        ),
        (
            "content_card",
            "home",
            "content_detail",
            {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.4},
            {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.40001},
        ),
        (
            "profile_payment",
            "profile",
            "payment",
            {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.15},
            {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.15001},
        ),
    ],
)
def test_visual_target_geometry_limits_are_inclusive_at_exact_boundary(
    target_kind: str,
    source_page_type: str,
    expected_page_type: str,
    boundary_rect: dict[str, float],
    excess_rect: dict[str, float],
) -> None:
    """几何阈值允许精确边界，但拒绝刚刚越过面积上限的候选。"""

    def evaluate(rect: dict[str, float]):
        return evaluate_visual_navigation_target(
            target_kind=target_kind,
            source_page_type=source_page_type,
            expected_page_type=expected_page_type,
            visual_target_confidence=0.99,
            selected=False,
            risk_flags=(),
            source_page_type_confidence=0.95,
            source_identity_bound=True,
            normalized_rect=rect,
        )

    assert evaluate(boundary_rect).allowed is True
    excess = evaluate(excess_rect)
    assert excess.allowed is False
    assert excess.reason_code == "visual_target_geometry_invalid"


def test_visual_region_digests_bind_local_and_surrounding_pixels_independently() -> None:
    """局部像素与周边锚点任一变化都必须改变各自摘要。"""
    width = height = 10
    baseline = bytearray([20, 30, 40] * width * height)
    local_changed = bytearray(baseline)
    anchor_changed = bytearray(baseline)
    local_changed[(5 * width + 5) * 3 : (5 * width + 5) * 3 + 3] = b"\xff\x00\x00"
    anchor_changed[(3 * width + 3) * 3 : (3 * width + 3) * 3 + 3] = b"\x00\xff\x00"
    rect = {"x": 0.4, "y": 0.4, "width": 0.2, "height": 0.2}

    baseline_digests = visual_navigation_region_digests(
        pixel_bytes=bytes(baseline),
        image_width=width,
        image_height=height,
        channel_count=3,
        normalized_rect=rect,
    )
    local_digests = visual_navigation_region_digests(
        pixel_bytes=bytes(local_changed),
        image_width=width,
        image_height=height,
        channel_count=3,
        normalized_rect=rect,
    )
    anchor_digests = visual_navigation_region_digests(
        pixel_bytes=bytes(anchor_changed),
        image_width=width,
        image_height=height,
        channel_count=3,
        normalized_rect=rect,
    )

    assert local_digests["local_visual_digest"] != baseline_digests[
        "local_visual_digest"
    ]
    assert local_digests["surrounding_anchor_digest"] == baseline_digests[
        "surrounding_anchor_digest"
    ]
    assert anchor_digests["local_visual_digest"] == baseline_digests[
        "local_visual_digest"
    ]
    assert anchor_digests["surrounding_anchor_digest"] != baseline_digests[
        "surrounding_anchor_digest"
    ]


def test_visual_region_digests_accept_extremely_small_image_with_both_samples() -> None:
    """极小图只要目标和锚点都至少包含一个像素，仍应产生稳定摘要。"""
    digests = visual_navigation_region_digests(
        pixel_bytes=bytes([10, 20, 30, 255, 40, 50, 60, 128]),
        image_width=2,
        image_height=1,
        channel_count=4,
        normalized_rect={"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0},
    )

    assert digests["local_visual_digest"].startswith("sha256:")
    assert digests["surrounding_anchor_digest"].startswith("sha256:")


@pytest.mark.parametrize(
    ("image_width", "image_height", "rect"),
    [
        (1, 1, {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}),
        (2, 2, {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}),
    ],
)
def test_visual_region_digests_reject_empty_anchor_sample(
    image_width: int,
    image_height: int,
    rect: dict[str, float],
) -> None:
    """目标覆盖全部像素时周边锚点为空，能力必须失败关闭。"""
    with pytest.raises(VisualNavigationContractError, match="采样像素不能为空"):
        visual_navigation_region_digests(
            pixel_bytes=bytes([0, 0, 0, 255] * image_width * image_height),
            image_width=image_width,
            image_height=image_height,
            channel_count=4,
            normalized_rect=rect,
        )
