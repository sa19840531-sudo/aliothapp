"""测试视觉导航最新帧复验与严格 CSS 坐标链。"""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pytest

from services.api.app.domain.exploration.h5_logical_surface import (
    build_h5_logical_surface_contract,
)


def _runtime() -> Any:
    return importlib.import_module(
        "services.api.app.infrastructure.browser.h5_visual_navigation_runtime"
    )


def _png(image: np.ndarray) -> bytes:
    encoded, payload = cv2.imencode(".png", image)
    assert encoded is True
    return payload.tobytes()


def _base_frame(width: int, height: int) -> np.ndarray:
    image = np.full((height, width, 3), 238, dtype=np.uint8)
    for x in range(12, width, 37):
        cv2.line(image, (x, 0), (x, height - 1), (224, 230, 236), 1)
    for y in range(9, height, 41):
        cv2.line(image, (0, y), (width - 1, y), (230, 222, 228), 1)
    cv2.rectangle(image, (3, 3), (width - 4, height - 4), (190, 198, 205), 2)
    return image


def _draw_target(image: np.ndarray, rect: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = rect
    cv2.rectangle(image, (left, top), (right - 1, bottom - 1), (38, 62, 186), -1)
    cv2.rectangle(image, (left + 3, top + 3), (right - 4, bottom - 4), (246, 199, 44), 2)
    cv2.line(image, (left + 7, top + 8), (right - 8, bottom - 9), (250, 250, 250), 3)
    cv2.line(image, (right - 9, top + 7), (left + 8, bottom - 8), (22, 24, 28), 2)
    cv2.circle(
        image,
        ((left + right) // 2, (top + bottom) // 2),
        max(3, min(right - left, bottom - top) // 7),
        (54, 210, 105),
        -1,
    )


def _frame(
    width: int,
    height: int,
    rect: tuple[int, int, int, int],
    *,
    extra_rects: tuple[tuple[int, int, int, int], ...] = (),
) -> np.ndarray:
    image = _base_frame(width, height)
    _draw_target(image, rect)
    for extra_rect in extra_rects:
        _draw_target(image, extra_rect)
    return image


def _capability(
    *,
    image_width: int,
    image_height: int,
    rect: tuple[int, int, int, int],
    viewport_width: float,
    viewport_height: float,
    offset_left: float = 0,
    offset_top: float = 0,
    page_left: float = 0,
    page_top: float = 0,
    scale: float = 1,
    transform: dict[str, object] | None = None,
) -> SimpleNamespace:
    left, top, right, bottom = rect
    return SimpleNamespace(
        normalized_rect={
            "x": left / image_width,
            "y": top / image_height,
            "width": (right - left) / image_width,
            "height": (bottom - top) / image_height,
        },
        source_image_size={"width": image_width, "height": image_height},
        viewport_metrics={
            "width": viewport_width,
            "height": viewport_height,
            "offset_left": offset_left,
            "offset_top": offset_top,
            "page_left": page_left,
            "page_top": page_top,
            "scale": scale,
        },
        model_image_transform=transform
        or {
            "kind": "identity",
            "scale_x": 1.0,
            "scale_y": 1.0,
            "offset_x": 0.0,
            "offset_y": 0.0,
        },
    )


def _viewport(
    *,
    width: float,
    height: float,
    offset_left: float = 0,
    offset_top: float = 0,
    page_left: float = 0,
    page_top: float = 0,
    scale: float = 1,
    device_pixel_ratio: float = 1,
    full_page: bool = False,
    flutter_host_transform: tuple[float, ...] = (1, 0, 0, 1, 0, 0),
) -> dict[str, object]:
    return {
        "width": width,
        "height": height,
        "offset_left": offset_left,
        "offset_top": offset_top,
        "page_left": page_left,
        "page_top": page_top,
        "scale": scale,
        "device_pixel_ratio": device_pixel_ratio,
        "full_page": full_page,
        "flutter_host_transform": flutter_host_transform,
    }


def _draw_transition_page(
    *,
    main_layout: str,
    active_slot: int | None = None,
) -> np.ndarray:
    """构造只依赖几何结构的稳定视觉页面，不以颜色表达选中态。"""
    image = np.full((320, 240, 3), 246, dtype=np.uint8)
    cv2.rectangle(image, (8, 8), (231, 45), (38, 38, 38), 2)
    cv2.line(image, (18, 26), (126, 26), (58, 58, 58), 3)
    if main_layout == "grid":
        for left, top in ((16, 62), (126, 62), (16, 152), (126, 152)):
            cv2.rectangle(image, (left, top), (left + 98, top + 72), (45, 45, 45), 2)
            cv2.line(image, (left + 8, top + 55), (left + 72, top + 55), (45, 45, 45), 2)
    elif main_layout == "list":
        for top in (62, 108, 154, 200):
            cv2.rectangle(image, (16, top), (223, top + 34), (45, 45, 45), 2)
            cv2.rectangle(image, (24, top + 7), (43, top + 26), (45, 45, 45), 2)
    elif main_layout == "detail":
        cv2.rectangle(image, (18, 58), (221, 166), (45, 45, 45), 3)
        for top, right in ((184, 214), (202, 196), (220, 224), (238, 168)):
            cv2.line(image, (22, top), (right, top), (45, 45, 45), 3)
    elif main_layout == "profile_menu":
        cv2.circle(image, (50, 82), 24, (45, 45, 45), 3)
        cv2.line(image, (86, 75), (196, 75), (45, 45, 45), 4)
        cv2.line(image, (86, 91), (160, 91), (45, 45, 45), 3)
        for top in (124, 160, 196, 232):
            cv2.rectangle(image, (18, top), (221, top + 25), (45, 45, 45), 2)
    elif main_layout == "settings_form":
        for top in (62, 104, 146, 188, 230):
            cv2.rectangle(image, (18, top), (221, top + 29), (45, 45, 45), 2)
            cv2.rectangle(image, (28, top + 8), (40, top + 20), (45, 45, 45), 2)
            cv2.line(image, (190, top + 14), (208, top + 14), (45, 45, 45), 2)
    elif main_layout == "payment_panel":
        cv2.rectangle(image, (24, 58), (215, 146), (45, 45, 45), 3)
        cv2.circle(image, (120, 101), 22, (45, 45, 45), 3)
        for top in (170, 204):
            cv2.rectangle(image, (24, top), (215, top + 24), (45, 45, 45), 2)
        cv2.rectangle(image, (54, 244), (185, 269), (45, 45, 45), 3)
    elif main_layout == "activity_dashboard":
        cv2.rectangle(image, (18, 60), (221, 111), (45, 45, 45), 3)
        for left, top in ((18, 128), (124, 128), (18, 190), (124, 190)):
            cv2.rectangle(image, (left, top), (left + 97, top + 46), (45, 45, 45), 2)
    else:
        raise AssertionError(f"未知主布局：{main_layout}")

    cv2.line(image, (0, 279), (239, 279), (38, 38, 38), 2)
    for index in range(4):
        center_x = 30 + index * 60
        cv2.circle(image, (center_x, 294), 7, (45, 45, 45), 2)
        cv2.line(image, (center_x - 10, 306), (center_x + 10, 306), (45, 45, 45), 2)
        if index == active_slot:
            cv2.rectangle(
                image,
                (center_x - 18, 313),
                (center_x + 18, 317),
                (20, 20, 20),
                -1,
            )
    return image


def _visual_layout_identity(
    *,
    main_layout: str,
    active_slot: int | None,
) -> dict[str, object]:
    return {
        "top_structure": {"category": "app_bar", "segment_count": 1},
        "bottom_navigation": {"slot_count": 4, "active_slot": active_slot},
        "main_layout_category": main_layout,
    }


def _transition_surface(
    *,
    page_type: str,
    main_layout: str,
    active_slot: int | None,
    state_signals: dict[str, object] | None = None,
    visual_layout: dict[str, object] | None = None,
) -> dict[str, object]:
    surface = build_h5_logical_surface_contract(
        render_profile={
            "kind": "flutter_web",
            "status": "confirmed",
            "confidence": 0.96,
        },
        document_identity_material=("session-1", "document-1"),
        identity_signals={
            "active_navigation": [],
            "page_heading": [],
            "main_structure": [],
            "stable_controls": [],
            "semantics_root": {"role_sequence": []},
            "visual_layout": visual_layout
            or _visual_layout_identity(
                main_layout=main_layout,
                active_slot=active_slot,
            ),
        },
        state_signals=state_signals or {},
        evidence_sources=("visual_layout",),
        evidence_quality="medium",
        degraded_reasons=(),
    )
    surface["semantics_coverage"] = {"level": "semantics_sparse"}
    surface["page_type_binding"] = {
        "page_type": page_type,
        "identity_fingerprint": surface["identity_fingerprint"],
        "page_type_confidence": 0.91,
        "coverage_allowed": True,
    }
    return surface


def _transition_capability(
    *,
    target_kind: str,
    expected_page_type: str,
    source_page_type: str,
    normalized_rect: dict[str, float] | None = None,
    source_png: bytes | None = None,
    before_surface: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        target_kind=target_kind,
        expected_page_type=expected_page_type,
        source_page_type=source_page_type,
        normalized_rect=normalized_rect
        or {"x": 0.5, "y": 0.875, "width": 0.25, "height": 0.125},
        source_artifact_sha256=(
            f"sha256:{hashlib.sha256(source_png).hexdigest()}"
            if source_png is not None
            else ""
        ),
        source_identity_fingerprint=(
            str(before_surface.get("identity_fingerprint") or "")
            if before_surface is not None
            else ""
        ),
        source_state_fingerprint=(
            str(before_surface.get("state_fingerprint") or "")
            if before_surface is not None
            else ""
        ),
    )


def _alternate_detail_transition_page() -> np.ndarray:
    """生成与详情类别一致、但主体边缘结构明显不同的来源帧。"""
    image = np.full((320, 240, 3), 246, dtype=np.uint8)
    cv2.rectangle(image, (8, 8), (231, 45), (38, 38, 38), 2)
    cv2.line(image, (18, 26), (126, 26), (58, 58, 58), 3)
    cv2.rectangle(image, (22, 64), (217, 152), (45, 45, 45), 3)
    for top, left, right in (
        (178, 28, 220),
        (198, 28, 178),
        (218, 48, 214),
        (242, 28, 188),
    ):
        cv2.line(image, (left, top), (right, top), (45, 45, 45), 3)
    cv2.line(image, (0, 279), (239, 279), (38, 38, 38), 2)
    for index in range(4):
        center_x = 30 + index * 60
        cv2.circle(image, (center_x, 294), 7, (45, 45, 45), 2)
        cv2.line(image, (center_x - 10, 306), (center_x + 10, 306), (45, 45, 45), 2)
        if index == 0:
            cv2.rectangle(
                image,
                (center_x - 18, 313),
                (center_x + 18, 317),
                (20, 20, 20),
                -1,
            )
    return image


def _draw_obstructed_transition_page(kind: str) -> np.ndarray:
    """构造稳定但只代表遮罩、广告或主题填色的伪目的地。"""
    if kind == "profile_overlay":
        image = np.full((320, 240, 3), 205, dtype=np.uint8)
        cv2.rectangle(image, (14, 50), (225, 273), (248, 248, 248), -1)
        for top in (62, 102, 142, 182, 222):
            cv2.rectangle(image, (24, top), (215, top + 27), (45, 45, 45), 2)
    elif kind == "payment_overlay":
        image = np.full((320, 240, 3), 205, dtype=np.uint8)
        cv2.rectangle(image, (14, 50), (225, 273), (248, 248, 248), -1)
        cv2.rectangle(image, (24, 58), (215, 146), (45, 45, 45), 3)
        for top in (170, 204):
            cv2.rectangle(image, (24, top), (215, top + 24), (45, 45, 45), 2)
        cv2.rectangle(image, (54, 244), (185, 269), (45, 45, 45), 3)
    else:
        image = _draw_transition_page(main_layout="grid", active_slot=0)
        if kind == "fullscreen_ad":
            cv2.rectangle(image, (4, 50), (235, 277), (35, 35, 35), -1)
            cv2.rectangle(image, (20, 68), (219, 242), (218, 218, 218), 3)
            cv2.line(image, (42, 214), (198, 86), (218, 218, 218), 5)
        elif kind == "large_toast":
            cv2.rectangle(image, (25, 105), (215, 190), (30, 30, 30), -1)
        elif kind == "stable_spinner":
            cv2.rectangle(image, (0, 48), (239, 278), (70, 70, 70), -1)
            cv2.circle(image, (120, 158), 25, (225, 225, 225), 5)
        elif kind == "dark_rectangle":
            cv2.rectangle(image, (18, 62), (222, 242), (18, 18, 18), -1)
        elif kind == "theme_fill":
            image[48:279] = (160, 160, 160)
            cv2.rectangle(image, (16, 62), (114, 134), (45, 45, 45), 2)
            cv2.rectangle(image, (126, 62), (224, 134), (45, 45, 45), 2)
            cv2.rectangle(image, (16, 152), (114, 224), (45, 45, 45), 2)
            cv2.rectangle(image, (126, 152), (224, 224), (45, 45, 45), 2)
        else:
            raise AssertionError(f"未知遮罩类型：{kind}")
    cv2.line(image, (0, 279), (239, 279), (38, 38, 38), 2)
    return image


def _draw_dark_detail_transition_page(*, solid_hero: bool) -> np.ndarray:
    """构造正常深色主题详情；实心媒体区不代表遮罩。"""
    image = np.full((320, 240, 3), 22, dtype=np.uint8)
    cv2.rectangle(image, (8, 8), (231, 45), (225, 225, 225), 2)
    cv2.line(image, (18, 26), (126, 26), (215, 215, 215), 3)
    if solid_hero:
        cv2.rectangle(image, (18, 58), (221, 166), (58, 66, 78), -1)
        cv2.rectangle(image, (18, 58), (221, 166), (225, 225, 225), 3)
    else:
        cv2.rectangle(image, (18, 58), (221, 166), (225, 225, 225), 3)
        cv2.line(image, (34, 78), (202, 148), (145, 180, 220), 3)
    for top, right in ((184, 214), (202, 196), (220, 224), (238, 168)):
        cv2.line(image, (22, top), (right, top), (225, 225, 225), 3)
    cv2.line(image, (0, 279), (239, 279), (225, 225, 225), 2)
    for index in range(4):
        center_x = 30 + index * 60
        cv2.circle(image, (center_x, 294), 7, (225, 225, 225), 2)
        cv2.line(
            image,
            (center_x - 10, 306),
            (center_x + 10, 306),
            (225, 225, 225),
            2,
        )
        if index == 0:
            cv2.rectangle(
                image,
                (center_x - 18, 313),
                (center_x + 18, 317),
                (225, 225, 225),
                -1,
            )
    return image


def _draw_high_texture_fullscreen_ad() -> np.ndarray:
    """构造有标题、文案和 CTA、但没有页面级 chrome/back 的高纹理广告。"""
    generator = np.random.default_rng(20260829)
    image = generator.integers(25, 225, size=(320, 240, 3), dtype=np.uint8)
    image = cv2.GaussianBlur(image, (5, 5), 0)
    cv2.rectangle(image, (14, 62), (225, 170), (238, 238, 238), 3)
    for top, right in ((190, 218), (210, 188), (230, 220)):
        cv2.line(image, (24, top), (right, top), (245, 245, 245), 4)
    cv2.rectangle(image, (58, 248), (182, 274), (245, 245, 245), 3)
    return image


def _draw_forged_detail_fullscreen_ad() -> np.ndarray:
    """构造伪造 app-bar/back、媒体、五行正文和 CTA 的整页广告。"""
    generator = np.random.default_rng(20260830)
    image = generator.integers(38, 218, size=(320, 240, 3), dtype=np.uint8)
    image = cv2.GaussianBlur(image, (7, 7), 0)
    image[:54] = (242, 242, 242)
    image[280:] = (242, 242, 242)
    cv2.rectangle(image, (8, 8), (231, 45), (38, 38, 38), 2)
    cv2.line(image, (18, 26), (28, 18), (38, 38, 38), 3)
    cv2.line(image, (18, 26), (28, 34), (38, 38, 38), 3)
    cv2.line(image, (42, 26), (88, 26), (38, 38, 38), 3)
    cv2.rectangle(image, (18, 58), (221, 150), (245, 245, 245), 3)
    for top, right in (
        (168, 216),
        (186, 204),
        (204, 220),
        (222, 198),
        (240, 214),
    ):
        cv2.line(image, (22, top), (right, top), (245, 245, 245), 3)
    cv2.rectangle(image, (80, 252), (160, 272), (245, 245, 245), 3)
    return image


def _draw_shell_preserving_main_takeover_ad() -> np.ndarray:
    """精确复制原页面上下外壳，但用高纹理广告接管整个主体。"""
    image = _draw_transition_page(main_layout="grid", active_slot=0)
    generator = np.random.default_rng(20260831)
    takeover = generator.integers(35, 220, size=(231, 240, 3), dtype=np.uint8)
    image[48:279] = cv2.GaussianBlur(takeover, (7, 7), 0)
    cv2.rectangle(image, (18, 58), (221, 150), (245, 245, 245), 3)
    for top, right in (
        (168, 216),
        (186, 204),
        (204, 220),
        (222, 198),
        (240, 214),
    ):
        cv2.line(image, (22, top), (right, top), (245, 245, 245), 3)
    cv2.rectangle(image, (80, 252), (160, 272), (245, 245, 245), 3)
    return image


def test_extract_visual_layout_evidence_contains_only_quantized_structure() -> None:
    """加入整帧摘要、颜色、坐标或节点集合会污染逻辑页面身份。"""
    evidence = _runtime().extract_visual_layout_evidence(
        viewport_png=_png(
            _draw_transition_page(main_layout="grid", active_slot=0)
        )
    )

    assert evidence["protocol_version"] == "visual_layout_evidence.v1"
    assert evidence["identity"] == _visual_layout_identity(
        main_layout="grid",
        active_slot=0,
    )
    assert set(evidence) == {"protocol_version", "identity", "state_audit"}
    serialized_identity = json.dumps(evidence["identity"], sort_keys=True)
    for forbidden in ("hash", "digest", "color", "scroll", "animation", "coordinate", "node"):
        assert forbidden not in serialized_identity


@pytest.mark.parametrize(
    (
        "target_kind",
        "before_type",
        "before_layout",
        "after_type",
        "after_layout",
        "expected_type",
        "before_active",
        "after_active",
    ),
    [
        (
            "primary_navigation",
            "home",
            "grid",
            "video_list",
            "list",
            "video_list",
            0,
            2,
        ),
        (
            "content_card",
            "content_list",
            "grid",
            "content_detail",
            "detail",
            "content_detail",
            0,
            0,
        ),
        (
            "video_card",
            "video_list",
            "grid",
            "video_detail",
            "detail",
            "video_detail",
            1,
            1,
        ),
        (
            "profile_settings",
            "profile",
            "profile_menu",
            "settings",
            "settings_form",
            "settings",
            3,
            3,
        ),
        (
            "profile_payment",
            "profile",
            "profile_menu",
            "payment",
            "payment_panel",
            "payment",
            3,
            3,
        ),
        (
            "profile_activity",
            "profile",
            "profile_menu",
            "activity",
            "activity_dashboard",
            "activity",
            3,
            3,
        ),
    ],
)
def test_stable_visual_navigation_patterns_prove_logical_transition(
    target_kind: str,
    before_type: str,
    before_layout: str,
    after_type: str,
    after_layout: str,
    expected_type: str,
    before_active: int,
    after_active: int,
) -> None:
    """底栏、详情与 Profile 本地入口均须由两张稳定后帧主证明。"""
    before_png = _png(
        _draw_transition_page(
            main_layout=before_layout,
            active_slot=before_active,
        )
    )
    after_png = _png(
        _draw_transition_page(
            main_layout=after_layout,
            active_slot=after_active,
        )
    )

    transition = _runtime().assess_visual_navigation_transition(
        before_png=before_png,
        after_pngs=(after_png, after_png),
        capability=_transition_capability(
            target_kind=target_kind,
            expected_page_type=expected_type,
            source_page_type=before_type,
        ),
        before_surface=_transition_surface(
            page_type=before_type,
            main_layout=before_layout,
            active_slot=before_active,
        ),
        after_surface=_transition_surface(
            page_type=after_type,
            main_layout=after_layout,
            active_slot=after_active,
        ),
    )

    assert transition["transition_kind"] == "logical_page_changed"
    assert transition["logical_transition_proof"] == "trusted"
    assert transition["visual_proof"]["status"] == "trusted"
    assert transition["visual_proof"]["after_frame_count"] == 2
    serialized = json.dumps(transition["visual_proof"], ensure_ascii=False)
    for forbidden in ("png", "sha256", "signature", "coordinate", "ocr"):
        assert forbidden not in serialized.casefold()


def test_expected_page_type_is_intent_only_and_does_not_assign_after_page_type() -> None:
    """视觉主证明绑定 after identity 即可，不得借 expected_page_type 生成页型。"""
    before_surface = _transition_surface(
        page_type="content_list",
        main_layout="grid",
        active_slot=0,
    )
    after_surface = _transition_surface(
        page_type="content_detail",
        main_layout="detail",
        active_slot=0,
    )
    before_surface.pop("page_type_binding")
    after_surface.pop("page_type_binding")
    before = _png(_draw_transition_page(main_layout="grid", active_slot=0))
    after = _png(_draw_transition_page(main_layout="detail", active_slot=0))

    transition = _runtime().assess_visual_navigation_transition(
        before_png=before,
        after_pngs=(after, after),
        capability=_transition_capability(
            target_kind="content_card",
            expected_page_type="content_detail",
            source_page_type="content_list",
        ),
        before_surface=before_surface,
        after_surface=after_surface,
    )

    assert transition["logical_transition_proof"] == "trusted"
    assert "page_type_binding" not in after_surface
    assert "page_type" not in transition["visual_proof"]


def test_live_before_frame_makes_unchanged_current_page_no_effect() -> None:
    """来源帧与当前页同类别但结构已漂移时，B→B 不能被 A→B 冒充转换。"""
    source_png = _png(_alternate_detail_transition_page())
    live_before_png = _png(
        _draw_transition_page(main_layout="detail", active_slot=0)
    )
    before_surface = _transition_surface(
        page_type="content_detail",
        main_layout="detail",
        active_slot=0,
    )
    after_surface = _transition_surface(
        page_type="content_detail",
        main_layout="detail",
        active_slot=0,
    )

    transition = _runtime().assess_visual_navigation_transition(
        source_png=source_png,
        before_png=live_before_png,
        after_pngs=(live_before_png, live_before_png),
        capability=_transition_capability(
            target_kind="content_card",
            expected_page_type="content_detail",
            source_page_type="content_detail",
            source_png=source_png,
            before_surface=before_surface,
        ),
        before_surface=before_surface,
        after_surface=after_surface,
    )

    assert transition["visual_proof"]["status"] == "no_effect"
    assert transition["logical_transition_proof"] != "trusted"
    assert transition["visual_proof"]["reason_codes"] == [
        "visual_navigation_no_effect"
    ]


@pytest.mark.parametrize(
    "mismatch_kind",
    ["source_sha", "source_identity", "source_state", "source_visual_layout"],
)
def test_visual_transition_source_binding_mismatch_is_stale(
    mismatch_kind: str,
) -> None:
    """来源 bytes、逻辑身份/状态或视觉类别任一失配都不得产生 trusted proof。"""
    source_image = _draw_transition_page(main_layout="grid", active_slot=0)
    live_before_image = source_image.copy()
    if mismatch_kind == "source_visual_layout":
        source_image = _draw_transition_page(main_layout="list", active_slot=0)
    source_png = _png(source_image)
    live_before_png = _png(live_before_image)
    after_png = _png(_draw_transition_page(main_layout="detail", active_slot=0))
    before_surface = _transition_surface(
        page_type="content_list",
        main_layout="grid",
        active_slot=0,
    )
    after_surface = _transition_surface(
        page_type="content_detail",
        main_layout="detail",
        active_slot=0,
    )
    capability = _transition_capability(
        target_kind="content_card",
        expected_page_type="content_detail",
        source_page_type="content_list",
        source_png=source_png,
        before_surface=before_surface,
    )
    if mismatch_kind == "source_sha":
        capability.source_artifact_sha256 = "sha256:" + "f" * 64
    elif mismatch_kind == "source_identity":
        capability.source_identity_fingerprint = "sha256:" + "e" * 64
    elif mismatch_kind == "source_state":
        capability.source_state_fingerprint = "sha256:" + "d" * 64

    transition = _runtime().assess_visual_navigation_transition(
        source_png=source_png,
        before_png=live_before_png,
        after_pngs=(after_png, after_png),
        capability=capability,
        before_surface=before_surface,
        after_surface=after_surface,
    )

    assert transition["visual_proof"]["status"] == "stale"
    assert transition["logical_transition_proof"] != "trusted"
    assert any(
        code.startswith("visual_source_")
        for code in transition["visual_proof"]["reason_codes"]
    )


@pytest.mark.parametrize(
    ("obstruction_kind", "target_kind", "before_type", "before_layout", "after_layout"),
    [
        ("fullscreen_ad", "content_card", "content_list", "grid", "detail"),
        ("large_toast", "content_card", "content_list", "grid", "detail"),
        ("stable_spinner", "content_card", "content_list", "grid", "detail"),
        ("dark_rectangle", "content_card", "content_list", "grid", "detail"),
        (
            "profile_overlay",
            "profile_settings",
            "profile",
            "profile_menu",
            "settings_form",
        ),
        (
            "payment_overlay",
            "profile_payment",
            "profile",
            "profile_menu",
            "payment_panel",
        ),
    ],
)
def test_stable_obstruction_cannot_become_visual_destination(
    obstruction_kind: str,
    target_kind: str,
    before_type: str,
    before_layout: str,
    after_layout: str,
) -> None:
    """稳定广告、遮罩和模态层不能因为两帧一致而升级为目的地。"""
    before_png = _png(
        _draw_transition_page(main_layout=before_layout, active_slot=3)
    )
    after_png = _png(_draw_obstructed_transition_page(obstruction_kind))
    before_surface = _transition_surface(
        page_type=before_type,
        main_layout=before_layout,
        active_slot=3,
    )
    after_surface = _transition_surface(
        page_type="destination",
        main_layout=after_layout,
        active_slot=None,
    )
    expected_type = {
        "content_card": "content_detail",
        "profile_settings": "settings",
        "profile_payment": "payment",
    }[target_kind]

    transition = _runtime().assess_visual_navigation_transition(
        source_png=before_png,
        before_png=before_png,
        after_pngs=(after_png, after_png),
        capability=_transition_capability(
            target_kind=target_kind,
            expected_page_type=expected_type,
            source_page_type=before_type,
            source_png=before_png,
            before_surface=before_surface,
        ),
        before_surface=before_surface,
        after_surface=after_surface,
    )

    assert transition["logical_transition_proof"] != "trusted"
    assert "visual_destination_obstructed" in transition["visual_proof"][
        "reason_codes"
    ]


def test_theme_fill_does_not_change_visual_layout_category() -> None:
    """纯主题填色属于状态事实，不能把既有网格重新解释成详情页。"""
    before = _runtime().extract_visual_layout_evidence(
        viewport_png=_png(_draw_transition_page(main_layout="grid", active_slot=0))
    )
    after = _runtime().extract_visual_layout_evidence(
        viewport_png=_png(_draw_obstructed_transition_page("theme_fill"))
    )

    assert after["identity"]["main_layout_category"] == (
        before["identity"]["main_layout_category"]
    )
    assert after["state_audit"]["obstruction_bucket"] == "transition_context_required"


@pytest.mark.parametrize("layout", ["detail", "settings_form", "payment_panel"])
def test_real_destination_structures_are_not_obstructions(layout: str) -> None:
    """真实详情、设置和支付结构必须继续通过遮挡门禁。"""
    evidence = _runtime().extract_visual_layout_evidence(
        viewport_png=_png(_draw_transition_page(main_layout=layout, active_slot=0))
    )

    assert evidence["identity"]["main_layout_category"] == layout
    assert evidence["state_audit"]["obstruction_bucket"] == (
        "transition_context_required"
    )


@pytest.mark.parametrize("solid_hero", [False, True], ids=("dark-detail", "solid-hero"))
def test_dark_and_solid_hero_details_remain_trusted(solid_hero: bool) -> None:
    """正常深色主题与实心媒体头图不能被单一亮度/填充信号误杀。"""
    before_png = _png(_draw_transition_page(main_layout="grid", active_slot=0))
    after_png = _png(_draw_dark_detail_transition_page(solid_hero=solid_hero))

    transition = _runtime().assess_visual_navigation_transition(
        before_png=before_png,
        after_pngs=(after_png, after_png),
        capability=_transition_capability(
            target_kind="content_card",
            expected_page_type="content_detail",
            source_page_type="content_list",
        ),
        before_surface=_transition_surface(
            page_type="content_list",
            main_layout="grid",
            active_slot=0,
        ),
        after_surface=_transition_surface(
            page_type="content_detail",
            main_layout="detail",
            active_slot=0,
        ),
    )

    assert transition["logical_transition_proof"] == "trusted"
    assert transition["visual_proof"]["status"] == "trusted"
    assert "visual_destination_obstructed" not in transition["visual_proof"][
        "reason_codes"
    ]


def test_detail_without_bottom_navigation_uses_persistent_top_chrome() -> None:
    """无底栏详情仍可由 before→after 持续顶部 chrome 提供独立页面外框。"""
    before_image = _draw_transition_page(main_layout="grid", active_slot=0)
    after_image = _draw_dark_detail_transition_page(solid_hero=True)
    before_image[279:] = (246, 246, 246)
    after_image[279:] = (22, 22, 22)
    before_png = _png(before_image)
    after_png = _png(after_image)
    before_layout = _runtime().extract_visual_layout_evidence(
        viewport_png=before_png
    )["identity"]
    after_layout = _runtime().extract_visual_layout_evidence(
        viewport_png=after_png
    )["identity"]

    assert before_layout["bottom_navigation"]["slot_count"] == 0
    assert after_layout["bottom_navigation"]["slot_count"] == 0
    transition = _runtime().assess_visual_navigation_transition(
        before_png=before_png,
        after_pngs=(after_png, after_png),
        capability=_transition_capability(
            target_kind="content_card",
            expected_page_type="content_detail",
            source_page_type="content_list",
        ),
        before_surface=_transition_surface(
            page_type="content_list",
            main_layout="grid",
            active_slot=None,
            visual_layout=before_layout,
        ),
        after_surface=_transition_surface(
            page_type="content_detail",
            main_layout="detail",
            active_slot=None,
            visual_layout=after_layout,
        ),
    )

    assert transition["logical_transition_proof"] == "trusted"
    assert transition["visual_proof"]["reason_codes"] == []


@pytest.mark.parametrize("negative_kind", ["missing_chrome_ad", "surface_ad_signal"])
def test_ad_like_destination_never_becomes_trusted(negative_kind: str) -> None:
    """高纹理广告或独立 ad/modal 信号不得伪装成稳定详情目的地。"""
    before_png = _png(_draw_transition_page(main_layout="grid", active_slot=0))
    after_image = (
        _draw_high_texture_fullscreen_ad()
        if negative_kind == "missing_chrome_ad"
        else _draw_transition_page(main_layout="detail", active_slot=0)
    )
    after_png = _png(after_image)
    after_state = (
        {}
        if negative_kind == "missing_chrome_ad"
        else {
            "visual_obstruction_summary": {
                "ad_count": 1,
                "modal_count": 0,
                "close_only": True,
                "outbound_action_count": 1,
            }
        }
    )

    transition = _runtime().assess_visual_navigation_transition(
        before_png=before_png,
        after_pngs=(after_png, after_png),
        capability=_transition_capability(
            target_kind="content_card",
            expected_page_type="content_detail",
            source_page_type="content_list",
        ),
        before_surface=_transition_surface(
            page_type="content_list",
            main_layout="grid",
            active_slot=0,
        ),
        after_surface=_transition_surface(
            page_type="content_detail",
            main_layout="detail",
            active_slot=0,
            state_signals=after_state,
        ),
    )

    assert transition["logical_transition_proof"] != "trusted"
    assert transition["visual_proof"]["status"] == "insufficient"
    assert any(
        code in transition["visual_proof"]["reason_codes"]
        for code in (
            "detail_destination_structure_insufficient",
            "visual_destination_obstructed",
        )
    )


def test_forged_detail_fullscreen_ad_cannot_self_certify_destination() -> None:
    """伪造完整详情几何的整页广告不能靠 after 自身结构豁免接管拓扑。"""
    before_png = _png(_draw_transition_page(main_layout="grid", active_slot=0))
    after_png = _png(_draw_forged_detail_fullscreen_ad())
    extracted = _runtime().extract_visual_layout_evidence(viewport_png=after_png)

    assert extracted["identity"]["main_layout_category"] == "detail"
    facts = extracted["state_audit"]["destination_facts"]
    assert facts["page_chrome"] == "app_bar"
    assert facts["large_region_count"] >= 1
    assert facts["body_band_count"] >= 5

    transition = _runtime().assess_visual_navigation_transition(
        before_png=before_png,
        after_pngs=(after_png, after_png),
        capability=_transition_capability(
            target_kind="content_card",
            expected_page_type="content_detail",
            source_page_type="content_list",
        ),
        before_surface=_transition_surface(
            page_type="content_list",
            main_layout="grid",
            active_slot=0,
        ),
        after_surface=_transition_surface(
            page_type="content_detail",
            main_layout="detail",
            active_slot=None,
            visual_layout=extracted["identity"],
        ),
    )

    assert transition["logical_transition_proof"] != "trusted"
    assert transition["visual_proof"]["status"] == "insufficient"
    assert "visual_destination_obstructed" in transition["visual_proof"][
        "reason_codes"
    ]


def test_preserved_shell_cannot_exempt_main_region_takeover() -> None:
    """外框证明只能佐证页面，不能豁免高纹理主体接管。"""
    before_png = _png(_draw_transition_page(main_layout="grid", active_slot=0))
    after_png = _png(_draw_shell_preserving_main_takeover_ad())
    extracted = _runtime().extract_visual_layout_evidence(viewport_png=after_png)

    assert extracted["identity"]["top_structure"]["category"] == "app_bar"
    assert extracted["identity"]["bottom_navigation"] == {
        "slot_count": 4,
        "active_slot": 0,
    }
    assert extracted["identity"]["main_layout_category"] == "detail"
    facts = extracted["state_audit"]["destination_facts"]
    assert facts["large_region_count"] >= 1
    assert facts["body_band_count"] >= 5

    transition = _runtime().assess_visual_navigation_transition(
        before_png=before_png,
        after_pngs=(after_png, after_png),
        capability=_transition_capability(
            target_kind="content_card",
            expected_page_type="content_detail",
            source_page_type="content_list",
        ),
        before_surface=_transition_surface(
            page_type="content_list",
            main_layout="grid",
            active_slot=0,
        ),
        after_surface=_transition_surface(
            page_type="content_detail",
            main_layout="detail",
            active_slot=0,
            visual_layout=extracted["identity"],
        ),
    )

    assert transition["logical_transition_proof"] != "trusted"
    assert transition["visual_proof"]["status"] == "insufficient"
    assert "visual_destination_obstructed" in transition["visual_proof"][
        "reason_codes"
    ]


@pytest.mark.parametrize(
    "change_kind",
    [
        "click_ripple_only",
        "toast_only",
        "loading_spinner",
        "single_changed_frame",
        "button_color_only",
    ],
)
def test_transient_visual_changes_do_not_prove_navigation(change_kind: str) -> None:
    """局部瞬态、纯颜色与单帧差异都不能增加页面覆盖。"""
    before = _draw_transition_page(main_layout="grid", active_slot=0)
    after_1 = before.copy()
    after_2 = before.copy()
    if change_kind == "click_ripple_only":
        cv2.circle(after_1, (120, 145), 18, (40, 40, 40), 2)
    elif change_kind == "toast_only":
        cv2.rectangle(after_1, (65, 125), (175, 154), (30, 30, 30), -1)
        cv2.rectangle(after_2, (65, 125), (175, 154), (30, 30, 30), -1)
    elif change_kind == "loading_spinner":
        cv2.circle(after_1, (120, 145), 16, (30, 30, 30), 3)
        cv2.circle(after_2, (120, 145), 16, (30, 30, 30), 3)
    elif change_kind == "single_changed_frame":
        after_1 = _draw_transition_page(main_layout="detail", active_slot=0)
    elif change_kind == "button_color_only":
        after_1[64:160, 24:168] = (210, 160, 80)
        after_2[64:160, 24:168] = (210, 160, 80)
    else:
        raise AssertionError(f"未知变化：{change_kind}")

    transition = _runtime().assess_visual_navigation_transition(
        before_png=_png(before),
        after_pngs=(_png(after_1), _png(after_2)),
        capability=_transition_capability(
            target_kind="content_card",
            expected_page_type="content_detail",
            source_page_type="content_list",
            normalized_rect={"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.25},
        ),
        before_surface=_transition_surface(
            page_type="content_list",
            main_layout="grid",
            active_slot=0,
        ),
        after_surface=_transition_surface(
            page_type="content_detail",
            main_layout="grid",
            active_slot=0,
        ),
    )

    assert transition["logical_transition_proof"] != "trusted"
    assert transition["transition_kind"] in {
        "surface_state_changed",
        "uncertain",
        "unchanged",
    }


def test_inconsistent_after_frames_fail_even_with_dom_and_document_change() -> None:
    """URL、DOM 与 Semantics 辅证不得覆盖视觉两帧失败。"""
    before = _draw_transition_page(main_layout="grid", active_slot=0)
    after_1 = _draw_transition_page(main_layout="detail", active_slot=0)
    after_2 = _draw_transition_page(main_layout="list", active_slot=2)
    before_surface = _transition_surface(
        page_type="content_list",
        main_layout="grid",
        active_slot=0,
    )
    after_surface = _transition_surface(
        page_type="content_detail",
        main_layout="detail",
        active_slot=0,
    )
    after_surface["document_identity"] = "sha256:" + "9" * 64
    after_surface["identity_signals"]["page_heading"] = [{"label": "详情"}]
    after_surface["identity_signals"]["main_structure"] = [{"role": "article"}]

    transition = _runtime().assess_visual_navigation_transition(
        before_png=_png(before),
        after_pngs=(_png(after_1), _png(after_2)),
        capability=_transition_capability(
            target_kind="content_card",
            expected_page_type="content_detail",
            source_page_type="content_list",
        ),
        before_surface=before_surface,
        after_surface=after_surface,
    )

    assert transition["logical_transition_proof"] != "trusted"
    assert transition["visual_proof"]["status"] == "insufficient"
    assert transition["visual_proof"]["after_frames_stable"] is False


@pytest.mark.parametrize(
    ("image_size", "viewport_size", "rect", "viewport", "expected"),
    [
        (
            (400, 746),
            (400, 746),
            (100, 600, 180, 680),
            _viewport(width=400, height=746),
            (140.0, 640.0),
        ),
        (
            (390, 844),
            (390, 844),
            (100, 700, 180, 780),
            _viewport(width=390, height=844),
            (140.0, 740.0),
        ),
        (
            (375, 812),
            (375, 812),
            (75, 650, 150, 730),
            _viewport(
                width=375,
                height=812,
                flutter_host_transform=(0.5, 0, 0, 0.5, 0, 0),
            ),
            (112.5, 690.0),
        ),
        (
            (400, 746),
            (400, 746),
            (100, 600, 180, 680),
            _viewport(
                width=400,
                height=746,
                offset_left=12,
                offset_top=20,
            ),
            (152.0, 660.0),
        ),
        (
            (800, 1492),
            (400, 746),
            (200, 1200, 360, 1360),
            _viewport(width=400, height=746, device_pixel_ratio=2),
            (140.0, 640.0),
        ),
    ],
)
def test_coordinate_chain_maps_model_rect_to_visual_viewport_css_without_double_scale(
    image_size: tuple[int, int],
    viewport_size: tuple[int, int],
    rect: tuple[int, int, int, int],
    viewport: dict[str, object],
    expected: tuple[float, float],
) -> None:
    """错误应用 DPR 或 Flutter host scale 会让这些手算 CSS 坐标失败。"""
    image_width, image_height = image_size
    viewport_width, viewport_height = viewport_size
    png = _png(_frame(image_width, image_height, rect))
    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=png,
        current_png=png,
        capability=_capability(
            image_width=image_width,
            image_height=image_height,
            rect=rect,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            offset_left=float(viewport["offset_left"]),
            offset_top=float(viewport["offset_top"]),
        ),
        current_viewport=viewport,
    )

    assert resolution.css_x == pytest.approx(expected[0])
    assert resolution.css_y == pytest.approx(expected[1])
    assert resolution.relocated is False
    assert resolution.audit_codes == ()
    assert 0.99 <= resolution.match_score <= 1.0


@pytest.mark.parametrize(
    ("viewport_update", "expected_code"),
    [
        ({"full_page": True}, "VISUAL_TARGET_COORDINATE_INVALID"),
        ({"offset_left": 1}, "VISUAL_TARGET_COORDINATE_INVALID"),
        ({"device_pixel_ratio": 2}, "VISUAL_TARGET_COORDINATE_INVALID"),
        ({"width": 399}, "VISUAL_TARGET_COORDINATE_INVALID"),
        ({"page_top": 1}, "VISUAL_TARGET_COORDINATE_INVALID"),
    ],
)
def test_coordinate_chain_fails_closed_for_unsafe_current_viewport_changes(
    viewport_update: dict[str, object],
    expected_code: str,
) -> None:
    """全页帧或最新视口事实变化不能继续使用旧能力。"""
    rect = (100, 120, 180, 200)
    png = _png(_frame(400, 300, rect))
    viewport = {**_viewport(width=400, height=300), **viewport_update}

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=png,
        current_png=png,
        capability=_capability(
            image_width=400,
            image_height=300,
            rect=rect,
            viewport_width=400,
            viewport_height=300,
        ),
        current_viewport=viewport,
    )

    assert resolution.css_x is None
    assert resolution.css_y is None
    assert resolution.audit_codes == (expected_code,)


def test_coordinate_chain_rejects_non_identity_v1_model_transform() -> None:
    """v1 若接受 crop/resize 变换，原图矩形会被错误反算。"""
    rect = (100, 120, 180, 200)
    png = _png(_frame(400, 300, rect))

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=png,
        current_png=png,
        capability=_capability(
            image_width=400,
            image_height=300,
            rect=rect,
            viewport_width=400,
            viewport_height=300,
            transform={
                "kind": "identity",
                "scale_x": 0.5,
                "scale_y": 1.0,
                "offset_x": 0.0,
                "offset_y": 0.0,
            },
        ),
        current_viewport=_viewport(width=400, height=300),
    )

    assert resolution.audit_codes == ("VISUAL_TARGET_COORDINATE_INVALID",)


def test_coordinate_chain_combines_dpr_and_visual_viewport_scale_once() -> None:
    """截图像素比应等于 DPR×visualViewport.scale，且坐标只反算一次。"""
    rect = (200, 1200, 360, 1360)
    png = _png(_frame(800, 1600, rect))
    viewport = _viewport(
        width=200,
        height=400,
        offset_left=17,
        offset_top=23,
        scale=2,
        device_pixel_ratio=2,
    )
    viewport.update({"layout_width": 400, "layout_height": 800})

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=png,
        current_png=png,
        capability=_capability(
            image_width=800,
            image_height=1600,
            rect=rect,
            viewport_width=200,
            viewport_height=400,
            offset_left=17,
            offset_top=23,
            scale=2,
        ),
        current_viewport=viewport,
    )

    assert (resolution.css_x, resolution.css_y) == pytest.approx((87, 343))
    assert resolution.audit_codes == ()


@pytest.mark.parametrize(
    ("device_pixel_ratio", "image_size", "source_rect", "pixel_shift"),
    [
        (1, 240, (70, 80, 130, 140), 3),
        (2, 480, (140, 160, 260, 280), 6),
    ],
)
def test_drift_tolerance_is_equivalent_css_distance_across_dpr(
    device_pixel_ratio: int,
    image_size: int,
    source_rect: tuple[int, int, int, int],
    pixel_shift: int,
) -> None:
    """同为 1.5 CSS px 的漂移不得因 DPR 不同而被判成过期。"""
    left, top, right, bottom = source_rect
    moved_rect = (left + pixel_shift, top, right + pixel_shift, bottom)
    source_png = _png(_frame(image_size, image_size, source_rect))
    current_png = _png(_frame(image_size, image_size, moved_rect))
    viewport_size = image_size / (device_pixel_ratio * 2)

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=source_png,
        current_png=current_png,
        capability=_capability(
            image_width=image_size,
            image_height=image_size,
            rect=source_rect,
            viewport_width=viewport_size,
            viewport_height=viewport_size,
            scale=2,
        ),
        current_viewport=_viewport(
            width=viewport_size,
            height=viewport_size,
            scale=2,
            device_pixel_ratio=device_pixel_ratio,
        ),
    )

    assert resolution.audit_codes == ()
    assert resolution.relocated is False


@pytest.mark.parametrize(
    ("device_pixel_ratio", "image_size", "source_rect", "pixel_shift"),
    [
        (1, 240, (70, 80, 130, 140), 6),
        (2, 480, (140, 160, 260, 280), 12),
    ],
)
def test_drift_over_css_tolerance_is_stale_across_dpr(
    device_pixel_ratio: int,
    image_size: int,
    source_rect: tuple[int, int, int, int],
    pixel_shift: int,
) -> None:
    """同为 3 CSS px 的位移在不同 DPR 下都必须要求重新签发。"""
    left, top, right, bottom = source_rect
    moved_rect = (left + pixel_shift, top, right + pixel_shift, bottom)
    source_png = _png(_frame(image_size, image_size, source_rect))
    current_png = _png(_frame(image_size, image_size, moved_rect))
    viewport_size = image_size / (device_pixel_ratio * 2)

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=source_png,
        current_png=current_png,
        capability=_capability(
            image_width=image_size,
            image_height=image_size,
            rect=source_rect,
            viewport_width=viewport_size,
            viewport_height=viewport_size,
            scale=2,
        ),
        current_viewport=_viewport(
            width=viewport_size,
            height=viewport_size,
            scale=2,
            device_pixel_ratio=device_pixel_ratio,
        ),
    )

    assert resolution.css_x is None
    assert resolution.relocated is True
    assert resolution.audit_codes == (
        "VISUAL_TARGET_SOURCE_STATE_STALE",
        "relocation_candidate_found",
    )


def test_latest_frame_direct_match_returns_original_coordinate() -> None:
    """删除原位置复验会让直接匹配不再返回可执行 CSS 点。"""
    rect = (82, 92, 142, 152)
    png = _png(_frame(240, 240, rect))

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=png,
        current_png=png,
        capability=_capability(
            image_width=240,
            image_height=240,
            rect=rect,
            viewport_width=240,
            viewport_height=240,
        ),
        current_viewport=_viewport(width=240, height=240),
    )

    assert (resolution.css_x, resolution.css_y) == pytest.approx((112, 122))
    assert resolution.relocated is False
    assert resolution.audit_codes == ()


def test_exact_original_target_ignores_mild_dynamic_surroundings() -> None:
    """目标未变且周边锚点仍达标时，轻度外围刷新不得阻断直达点击。"""
    rect = (32, 42, 92, 102)
    source = _frame(320, 240, rect)
    _draw_target(source, (210, 120, 290, 200))
    current = source.copy()
    # 仅改远处小块，保留目标周边锚点结构，模拟无害动画而非整圈遮罩。
    cv2.rectangle(current, (200, 10), (310, 36), (12, 12, 12), -1)
    cv2.rectangle(current, (200, 200), (310, 230), (12, 12, 12), -1)

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=_png(source),
        current_png=_png(current),
        capability=_capability(
            image_width=320,
            image_height=240,
            rect=rect,
            viewport_width=320,
            viewport_height=240,
        ),
        current_viewport=_viewport(width=320, height=240),
    )

    assert (resolution.css_x, resolution.css_y) == pytest.approx((62, 72))
    assert resolution.relocated is False
    assert resolution.audit_codes == ()


def test_exact_target_with_changed_surrounding_anchor_fails_closed() -> None:
    """透明遮罩可保持目标像素不变，周边锚点变化时仍必须阻断点击。"""
    rect = (32, 42, 92, 102)
    source = _frame(320, 240, rect)
    current = source.copy()
    cv2.rectangle(current, (2, 12), (121, 41), (10, 10, 10), -1)
    cv2.rectangle(current, (2, 102), (121, 131), (10, 10, 10), -1)
    cv2.rectangle(current, (2, 42), (31, 101), (10, 10, 10), -1)
    cv2.rectangle(current, (92, 42), (121, 101), (10, 10, 10), -1)

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=_png(source),
        current_png=_png(current),
        capability=_capability(
            image_width=320,
            image_height=240,
            rect=rect,
            viewport_width=320,
            viewport_height=240,
        ),
        current_viewport=_viewport(width=320, height=240),
    )

    assert resolution.css_x is None
    assert resolution.css_y is None
    assert resolution.audit_codes == ("VISUAL_TARGET_SOURCE_STATE_STALE",)


def test_latest_frame_unique_eight_pixel_drift_requires_new_capability() -> None:
    """把唯一漂移直接改框点击会错误地让旧签名获得新位置权限。"""
    source_rect = (82, 92, 142, 152)
    moved_rect = (90, 100, 150, 160)
    source_png = _png(_frame(240, 240, source_rect))
    current_png = _png(_frame(240, 240, moved_rect))

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=source_png,
        current_png=current_png,
        capability=_capability(
            image_width=240,
            image_height=240,
            rect=source_rect,
            viewport_width=240,
            viewport_height=240,
        ),
        current_viewport=_viewport(width=240, height=240),
    )

    assert resolution.css_x is None
    assert resolution.css_y is None
    assert resolution.relocated is True
    assert resolution.audit_codes == (
        "VISUAL_TARGET_SOURCE_STATE_STALE",
        "relocation_candidate_found",
    )


def test_near_relocation_requires_anchor_at_shifted_candidate() -> None:
    """只有目标与周边 anchor 一起近距离平移，才可报告可信重定位候选。"""
    source_rect = (82, 92, 142, 152)
    moved_rect = (90, 100, 150, 160)
    source = _frame(240, 240, source_rect)
    current = _base_frame(240, 240)
    padding = 24
    source_patch = source[
        source_rect[1] - padding : source_rect[3] + padding,
        source_rect[0] - padding : source_rect[2] + padding,
    ]
    current[
        moved_rect[1] - padding : moved_rect[3] + padding,
        moved_rect[0] - padding : moved_rect[2] + padding,
    ] = source_patch

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=_png(source),
        current_png=_png(current),
        capability=_capability(
            image_width=240,
            image_height=240,
            rect=source_rect,
            viewport_width=240,
            viewport_height=240,
        ),
        current_viewport=_viewport(width=240, height=240),
    )

    assert resolution.relocated is True
    assert resolution.audit_codes == (
        "VISUAL_TARGET_SOURCE_STATE_STALE",
        "relocation_candidate_found",
    )


def test_near_matching_target_without_shifted_anchor_is_not_relocation_candidate() -> None:
    """新位置周边结构失配时，目标图案相同也不得报告 relocation。"""
    source_rect = (82, 92, 142, 152)
    moved_rect = (90, 100, 150, 160)
    source = _frame(240, 240, source_rect)
    current = _base_frame(240, 240)
    cv2.rectangle(current, (62, 72), (177, 187), (17, 17, 17), -1)
    _draw_target(current, moved_rect)

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=_png(source),
        current_png=_png(current),
        capability=_capability(
            image_width=240,
            image_height=240,
            rect=source_rect,
            viewport_width=240,
            viewport_height=240,
        ),
        current_viewport=_viewport(width=240, height=240),
    )

    assert resolution.css_x is None
    assert resolution.relocated is False
    assert resolution.audit_codes[0] in {
        "VISUAL_TARGET_SOURCE_STATE_STALE",
        "VISUAL_TARGET_OBSTRUCTED",
    }


def test_far_unique_matching_target_is_stale_without_relocation_candidate() -> None:
    """远处唯一相同卡片不能为旧能力证明位置漂移。"""
    source_rect = (32, 82, 92, 142)
    far_rect = (148, 82, 208, 142)
    source_png = _png(_frame(240, 240, source_rect))
    current_png = _png(_frame(240, 240, far_rect))

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=source_png,
        current_png=current_png,
        capability=_capability(
            image_width=240,
            image_height=240,
            rect=source_rect,
            viewport_width=240,
            viewport_height=240,
        ),
        current_viewport=_viewport(width=240, height=240),
    )

    assert resolution.css_x is None
    assert resolution.relocated is False
    assert resolution.audit_codes == ("VISUAL_TARGET_SOURCE_STATE_STALE",)


def test_two_far_matching_targets_remain_not_unique() -> None:
    """全图重复检测不能因候选都在漂移边界外而被跳过。"""
    source_rect = (22, 82, 82, 142)
    first_far_rect = (96, 82, 156, 142)
    second_far_rect = (170, 82, 230, 142)
    source_png = _png(_frame(240, 240, source_rect))
    current_png = _png(
        _frame(240, 240, first_far_rect, extra_rects=(second_far_rect,))
    )

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=source_png,
        current_png=current_png,
        capability=_capability(
            image_width=240,
            image_height=240,
            rect=source_rect,
            viewport_width=240,
            viewport_height=240,
        ),
        current_viewport=_viewport(width=240, height=240),
    )

    assert resolution.css_x is None
    assert resolution.relocated is False
    assert resolution.audit_codes == ("VISUAL_TARGET_NOT_UNIQUE",)


def test_latest_frame_rejects_two_equal_visual_targets() -> None:
    """只检查原位置会漏掉实时页面新增的同分目标。"""
    source_rect = (42, 92, 102, 152)
    duplicate_rect = (130, 92, 190, 152)
    source_png = _png(_frame(240, 240, source_rect))
    current_png = _png(
        _frame(240, 240, source_rect, extra_rects=(duplicate_rect,))
    )

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=source_png,
        current_png=current_png,
        capability=_capability(
            image_width=240,
            image_height=240,
            rect=source_rect,
            viewport_width=240,
            viewport_height=240,
        ),
        current_viewport=_viewport(width=240, height=240),
    )

    assert resolution.css_x is None
    assert resolution.css_y is None
    assert resolution.audit_codes == ("VISUAL_TARGET_NOT_UNIQUE",)


@pytest.mark.parametrize("mutation", ["covered", "area_changed"])
def test_latest_frame_rejects_obstruction_or_area_change(mutation: str) -> None:
    """遮挡或面积突变不能被普通低分漂移路径掩盖。"""
    rect = (82, 92, 142, 152)
    source = _frame(240, 240, rect)
    current = source.copy()
    if mutation == "covered":
        cv2.rectangle(current, (82, 92), (141, 151), (116, 116, 116), -1)
    else:
        cv2.rectangle(current, (72, 82), (151, 161), (38, 62, 186), -1)
        _draw_target(current, (72, 82, 152, 162))

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=_png(source),
        current_png=_png(current),
        capability=_capability(
            image_width=240,
            image_height=240,
            rect=rect,
            viewport_width=240,
            viewport_height=240,
        ),
        current_viewport=_viewport(width=240, height=240),
    )

    assert resolution.css_x is None
    assert resolution.css_y is None
    assert resolution.audit_codes == ("VISUAL_TARGET_OBSTRUCTED",)


def test_latest_frame_low_match_is_stale_without_exposing_image_material() -> None:
    """局部与锚点都失配时应稳定关闭，结果中不得带像素或摘要。"""
    rect = (82, 92, 142, 152)
    source_png = _png(_frame(240, 240, rect))
    rng = np.random.default_rng(20260829)
    current_png = _png(rng.integers(0, 256, size=(240, 240, 3), dtype=np.uint8))

    resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=source_png,
        current_png=current_png,
        capability=_capability(
            image_width=240,
            image_height=240,
            rect=rect,
            viewport_width=240,
            viewport_height=240,
        ),
        current_viewport=_viewport(width=240, height=240),
    )

    assert resolution.css_x is None
    assert resolution.css_y is None
    assert resolution.audit_codes == ("VISUAL_TARGET_SOURCE_STATE_STALE",)
    serialized = repr(asdict(resolution))
    assert "png" not in serialized.casefold()
    assert "digest" not in serialized.casefold()
    assert "pixel" not in serialized.casefold()


def test_new_capability_can_click_once_but_old_capability_cannot_follow_second_drift() -> None:
    """重观察后的新来源可命中，旧来源再次漂移仍只能要求重观察。"""
    old_rect = (74, 84, 134, 144)
    new_rect = (82, 92, 142, 152)
    next_rect = (90, 100, 150, 160)
    old_png = _png(_frame(240, 240, old_rect))
    new_png = _png(_frame(240, 240, new_rect))
    next_png = _png(_frame(240, 240, next_rect))
    viewport = _viewport(width=240, height=240)

    first_old_resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=old_png,
        current_png=new_png,
        capability=_capability(
            image_width=240,
            image_height=240,
            rect=old_rect,
            viewport_width=240,
            viewport_height=240,
        ),
        current_viewport=viewport,
    )
    new_resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=new_png,
        current_png=new_png,
        capability=_capability(
            image_width=240,
            image_height=240,
            rect=new_rect,
            viewport_width=240,
            viewport_height=240,
        ),
        current_viewport=viewport,
    )
    second_old_resolution = _runtime().resolve_visual_navigation_coordinate(
        source_png=new_png,
        current_png=next_png,
        capability=_capability(
            image_width=240,
            image_height=240,
            rect=new_rect,
            viewport_width=240,
            viewport_height=240,
        ),
        current_viewport=viewport,
    )

    assert first_old_resolution.css_x is None
    assert first_old_resolution.relocated is True
    assert new_resolution.audit_codes == ()
    assert (new_resolution.css_x, new_resolution.css_y) == pytest.approx((112, 122))
    assert second_old_resolution.css_x is None
    assert second_old_resolution.relocated is True
