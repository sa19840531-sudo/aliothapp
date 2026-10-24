"""Android 視覺 Agent：從 ActionIntent parameters 解析 canonical 座標。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.api.app.application.interaction_runtime import (
    ResolvedInteractionTarget,
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
)
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.device.adb_ops import (
    CANONICAL_HEIGHT,
    CANONICAL_WIDTH,
    CoordinateMappingError,
    DisplaySafeArea,
    MappedDevicePoint,
    assert_device_point_in_safe_area,
    map_point_to_device,
)
from services.api.app.infrastructure.device.android_runtime_observation import (
    AndroidBounds,
    AndroidRuntimeObservation,
)
from services.api.app.infrastructure.device.app_tap_audit_log import log_app_tap_audit

_SYSTEM_DIALOG_SAFE_MARKERS = (
    "下次",
    "稍后",
    "暂不",
    "拒绝",
    "不允许",
    "取消",
    "跳过",
    "以后",
    "dismiss",
    "deny",
    "decline",
    "not now",
    "later",
    "cancel",
    "skip",
    "close",
)
_SYSTEM_DIALOG_AUTHORIZATION_MARKERS = (
    "去开启",
    "立即开启",
    "允许",
    "授权",
    "allow",
    "grant",
    "open settings",
)
_SYSTEM_DIALOG_AUTHORIZATION_NEGATIONS = (
    "暂不",
    "暂时不",
    "不要",
    "不应",
    "不再",
    "不得",
    "不允许",
    "不授权",
    "无须",
    "拒绝",
    "禁止",
    "取消",
    "do not",
    "don't",
    "deny",
    "decline",
    "cancel",
)
_SYSTEM_DIALOG_AUTHORIZATION_CLAUSE_SEPARATORS = (
    "并",
    "然后",
    "且",
    "和",
    "以及",
    "同时",
    "，",
    "。",
    "；",
    ",",
    ";",
    "and",
    "then",
)
_POPUP_CLOSE_CONTEXT_MARKERS = (
    "关闭",
    "公告",
    "弹窗",
    "弹层",
    "popup",
    "dialog",
    "dismiss",
    "close",
)
# 整屏蒙层阈值：真实关闭控件不可能铺满整屏，命中整屏说明是可点穿的
# 「Dismiss」蒙层，取其中心会直接点在广告本体上。
_FULL_SURFACE_AXIS_RATIO = 0.9
_FULL_SURFACE_AREA_RATIO = 0.5


@dataclass(frozen=True, kw_only=True)
class ResolvedAndroidPointTarget:
    """Adapter 內部綁定：device_px 點、滑動軌道或鍵入文本。"""

    operation: InteractionOperation
    tap_x: int | None = None
    tap_y: int | None = None
    swipe_x1: int | None = None
    swipe_y1: int | None = None
    swipe_x2: int | None = None
    swipe_y2: int | None = None
    swipe_ms: int = 350
    input_text: str | None = None
    # 仅供设备边界写入动作审计；不进入 Agent 的公开动作合同。
    resolution_method: str = "parameters.point"
    resolved_bounds: AndroidBounds | None = None
    # letterbox／rotation_unproven 等换算审计字段。
    mapping_audit: Mapping[str, object] | None = None


def _mapping_audit_from_point(mapped: MappedDevicePoint) -> dict[str, object]:
    return {
        "letterboxed": mapped.letterboxed,
        "rotation_unproven": mapped.rotation_unproven,
        "display_rotation": mapped.display_rotation,
        "content_left": mapped.content_left,
        "content_top": mapped.content_top,
        "content_width": mapped.content_width,
        "content_height": mapped.content_height,
    }


class CanonicalPointTargetResolver:
    """把 parameters.point / scroll 起迄映射到 device_px；不做控件樹匹配。"""

    def __init__(
        self,
        *,
        device_width: int,
        device_height: int,
        safe_area: DisplaySafeArea | Mapping[str, object] | None = None,
        display_rotation: int | None = None,
    ) -> None:
        if device_width <= 0 or device_height <= 0:
            raise ValueError("device 尺寸必须为正")
        self._device_w = device_width
        self._device_h = device_height
        self._safe_area = (
            safe_area
            if isinstance(safe_area, DisplaySafeArea)
            else DisplaySafeArea.from_mapping(safe_area)
        )
        self._display_rotation = display_rotation

    @classmethod
    def from_device_ops(cls, ops: object) -> CanonicalPointTargetResolver:
        """从 AdbDeviceOps 读取尺寸、safe_area 与可证明 rotation。"""
        query_wm = getattr(ops, "query_wm_size", None)
        if callable(query_wm):
            try:
                query_wm()
            except Exception:  # noqa: BLE001 - 刷新失败时退回缓存 width/height
                pass
        width = int(ops.width)
        height = int(ops.height)
        safe_area: Mapping[str, object] | None = None
        dump = getattr(ops, "dump_display_safe_area", None)
        if callable(dump):
            try:
                raw = dump()
            except Exception:  # noqa: BLE001 - insets 旁路不得阻断落点换算
                raw = None
            if isinstance(raw, Mapping):
                safe_area = raw
        display_rotation: int | None = None
        query_rotation = getattr(ops, "query_display_rotation", None)
        if callable(query_rotation):
            try:
                display_rotation = query_rotation()
            except Exception:  # noqa: BLE001 - 旋转旁路失败视为未证明
                display_rotation = None
        # Physical 竖屏 + 已证明 90/270 时，校正为当前 Surface 坐标系。
        from services.api.app.infrastructure.device.adb_ops import (
            align_wm_size_to_current_surface,
        )

        width, height = align_wm_size_to_current_surface(
            width,
            height,
            display_rotation=display_rotation,
            wm_size_text="",
        )
        return cls(
            device_width=width,
            device_height=height,
            safe_area=safe_area,
            display_rotation=display_rotation,
        )

    async def resolve(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """滿足 SemanticTargetResolver 端口；實際 detail 由 resolve_android 提供。"""
        resolved = self.resolve_android(request)
        # selector 僅作進程內診斷字串，禁止回傳給 Agent
        if resolved.operation is InteractionOperation.TAP:
            selector = f"android-tap:{resolved.tap_x},{resolved.tap_y}"
        elif resolved.operation is InteractionOperation.SCROLL:
            selector = (
                f"android-swipe:{resolved.swipe_x1},{resolved.swipe_y1}"
                f"->{resolved.swipe_x2},{resolved.swipe_y2}"
            )
        elif resolved.operation is InteractionOperation.INPUT:
            selector = "android-input"
        else:
            selector = f"android-op:{resolved.operation.value}"
        return ResolvedInteractionTarget(
            selector=selector,
            input_text=resolved.input_text,
        )

    def resolve_android(
        self,
        request: SemanticTargetResolutionRequest,
        *,
        runtime_observation: AndroidRuntimeObservation | None = None,
    ) -> ResolvedAndroidPointTarget:
        op = request.action_type
        params: Mapping[str, Any] = request.parameters or {}
        # Explorer 合同用 CLICK；Android 原语统一为 TAP
        if op in {InteractionOperation.TAP, InteractionOperation.CLICK}:
            resolved = self._resolve_android_tap(
                request,
                params=params,
                runtime_observation=runtime_observation,
            )
            self._log_tap_resolve(request=request, params=params, resolved=resolved)
            return resolved
        if op is InteractionOperation.INPUT:
            return self._resolve_input(params)
        if op is InteractionOperation.SCROLL:
            return self._resolve_scroll(params)
        if op in {
            InteractionOperation.BACK,
            InteractionOperation.OBSERVE,
            InteractionOperation.TIMED_CAPTURE,
        }:
            return ResolvedAndroidPointTarget(operation=op)
        raise SemanticTargetResolutionError(
            f"Android Adapter 不支援 action_type={op.value}",
            error_code="ANDROID_OPERATION_UNSUPPORTED",
            retryable=False,
        )

    def _log_tap_resolve(
        self,
        *,
        request: SemanticTargetResolutionRequest,
        params: Mapping[str, Any],
        resolved: ResolvedAndroidPointTarget,
    ) -> None:
        source_point = params.get("point")
        if not isinstance(source_point, Mapping):
            source_point = params.get("resolved_target")
        source_x = source_y = source_space = None
        if isinstance(source_point, Mapping):
            source_x = source_point.get("x")
            source_y = source_point.get("y")
            source_space = source_point.get("space")
        bounds = resolved.resolved_bounds
        log_app_tap_audit(
            "resolve",
            action_type=getattr(request.action_type, "value", request.action_type),
            semantic_target=request.semantic_target,
            source_capture_id=request.source_capture_id,
            source_x=source_x,
            source_y=source_y,
            source_space=source_space,
            base_width=params.get("base_width"),
            base_height=params.get("base_height"),
            device_width=self._device_w,
            device_height=self._device_h,
            device_x=resolved.tap_x,
            device_y=resolved.tap_y,
            resolution_method=resolved.resolution_method,
            mapping_audit=resolved.mapping_audit,
            uia_bounds=(
                {
                    "left": bounds.left,
                    "top": bounds.top,
                    "right": bounds.right,
                    "bottom": bounds.bottom,
                }
                if bounds is not None
                else None
            ),
        )

    def _resolve_android_tap(
        self,
        request: SemanticTargetResolutionRequest,
        *,
        params: Mapping[str, Any],
        runtime_observation: AndroidRuntimeObservation | None,
    ) -> ResolvedAndroidPointTarget:
        semantic_target = _normalized_semantic_target(request.semantic_target)
        if semantic_target and runtime_observation is not None:
            # 账号凭证双钮：优先 UIA content-desc 精确命中，避免合同归一化框偏高点空。
            credential_label = _profile_credential_uia_label(params)
            if credential_label:
                semantic_target_node = _find_exact_clickable_node(
                    runtime_observation,
                    semantic_target=credential_label,
                )
                if semantic_target_node is not None:
                    x, y = _center_of_bounds(semantic_target_node.bounds)
                    if _point_inside_device(
                        x, y, width=self._device_w, height=self._device_h
                    ):
                        self._validate_safe_area(x, y)
                        return ResolvedAndroidPointTarget(
                            operation=InteractionOperation.TAP,
                            tap_x=x,
                            tap_y=y,
                            resolution_method=_uia_semantic_resolution_method(
                                semantic_target_node,
                                semantic_target=credential_label,
                            ),
                            resolved_bounds=semantic_target_node.bounds,
                        )
            # 弹层关层：阶段机已确认时优先唯一关闭控件，避免「确认关闭公告」
            # 被通用包含匹配抢成 semantic_unique 而丢掉 popup_close_alias。
            popup_aliases = _popup_close_control_aliases(
                semantic_target=semantic_target,
                parameters=params,
            )
            if popup_aliases:
                semantic_target_node = _find_unique_popup_close_node(
                    runtime_observation,
                    aliases=popup_aliases,
                )
                if semantic_target_node is not None:
                    x, y = _center_of_bounds(semantic_target_node.bounds)
                    self._validate_point(x, y)
                    return ResolvedAndroidPointTarget(
                        operation=InteractionOperation.TAP,
                        tap_x=x,
                        tap_y=y,
                        resolution_method="uiautomator.popup_close_alias",
                        resolved_bounds=semantic_target_node.bounds,
                    )
            semantic_target_node = _find_exact_clickable_node(
                runtime_observation,
                semantic_target=semantic_target,
            )
            if semantic_target_node is not None:
                x, y = _center_of_bounds(semantic_target_node.bounds)
                if _point_inside_device(x, y, width=self._device_w, height=self._device_h):
                    self._validate_safe_area(x, y)
                    return ResolvedAndroidPointTarget(
                        operation=InteractionOperation.TAP,
                        tap_x=x,
                        tap_y=y,
                        resolution_method=_uia_semantic_resolution_method(
                            semantic_target_node,
                            semantic_target=semantic_target,
                        ),
                        resolved_bounds=semantic_target_node.bounds,
                    )
                # 横屏 App 常见：UIA bounds 已是当前 Surface 坐标，但 resolver
                # 仍缓存 Physical 竖屏 wm size → 中心点 OOB。有 capture_px 时回退，
                # 禁止直接 ANDROID_POINT_OUT_OF_BOUNDS 卡死年龄门／弹窗清障。
                if _params_have_mappable_point(params):
                    mapped = self._resolve_mapped_point(params, field_name="point")
                    return ResolvedAndroidPointTarget(
                        operation=InteractionOperation.TAP,
                        tap_x=mapped.x,
                        tap_y=mapped.y,
                        resolution_method="capture_px.uia_oob_fallback",
                        mapping_audit=_mapping_audit_from_point(mapped),
                        resolved_bounds=semantic_target_node.bounds,
                    )
                self._validate_point(x, y)
            if _is_system_dialog_safe_alias_request(
                semantic_target=semantic_target,
                parameters=params,
            ):
                semantic_target_node = _find_unique_system_dialog_safe_node(
                    runtime_observation
                )
                if semantic_target_node is None:
                    raise SemanticTargetResolutionError(
                        "系统权限弹窗缺少唯一安全拒绝按钮",
                        error_code="ANDROID_TARGET_NOT_FOUND",
                        retryable=True,
                    )
                x, y = _center_of_bounds(semantic_target_node.bounds)
                self._validate_point(x, y)
                return ResolvedAndroidPointTarget(
                    operation=InteractionOperation.TAP,
                    tap_x=x,
                    tap_y=y,
                    resolution_method="uiautomator.safe_dialog_alias",
                    resolved_bounds=semantic_target_node.bounds,
                )
        mapped = self._resolve_mapped_point(params, field_name="point")
        return ResolvedAndroidPointTarget(
            operation=InteractionOperation.TAP,
            tap_x=mapped.x,
            tap_y=mapped.y,
            resolution_method=_point_resolution_method(params),
            mapping_audit=_mapping_audit_from_point(mapped),
        )

    def _resolve_input(self, params: Mapping[str, Any]) -> ResolvedAndroidPointTarget:
        text = params.get("text")
        if text is None:
            text = params.get("input_text")
        if not isinstance(text, str) or not text.strip():
            raise SemanticTargetResolutionError(
                "INPUT 缺少 text / input_text",
                error_code="ANDROID_INPUT_TEXT_MISSING",
                retryable=False,
            )
        tap_x = tap_y = None
        mapping_audit = None
        if isinstance(params.get("point"), Mapping) or (
            "x" in params and "y" in params
        ):
            mapped = self._resolve_mapped_point(params, field_name="point")
            tap_x, tap_y = mapped.x, mapped.y
            mapping_audit = _mapping_audit_from_point(mapped)
        return ResolvedAndroidPointTarget(
            operation=InteractionOperation.INPUT,
            tap_x=tap_x,
            tap_y=tap_y,
            input_text=text[:500],
            resolution_method=(
                _point_resolution_method(params)
                if tap_x is not None
                else "parameters.input"
            ),
            mapping_audit=mapping_audit,
        )

    def _resolve_mapped_point(
        self, params: Mapping[str, Any], *, field_name: str
    ) -> MappedDevicePoint:
        point = params.get(field_name)
        if not isinstance(point, Mapping):
            # 相容扁平 x/y
            if "x" in params and "y" in params:
                point = {
                    "x": params.get("x"),
                    "y": params.get("y"),
                    "space": (
                        params.get("coordinate_space")
                        or params.get("space")
                        or "canonical_px"
                    ),
                }
            else:
                raise SemanticTargetResolutionError(
                    f"缺少 {field_name} 座標",
                    error_code="ANDROID_POINT_MISSING",
                    retryable=False,
                )
        try:
            x = float(point["x"])
            y = float(point["y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SemanticTargetResolutionError(
                f"{field_name} 座標無效",
                error_code="ANDROID_POINT_INVALID",
                retryable=False,
            ) from exc
        space = str(point.get("space") or "canonical_px")
        display_raw = params.get("display")
        display = display_raw if isinstance(display_raw, Mapping) else {}
        base_w = int(display.get("width") or params.get("base_width") or CANONICAL_WIDTH)
        base_h = int(display.get("height") or params.get("base_height") or CANONICAL_HEIGHT)
        try:
            mapped = map_point_to_device(
                x,
                y,
                device_w=self._device_w,
                device_h=self._device_h,
                base_w=base_w,
                base_h=base_h,
                space=space,
                display_rotation=self._display_rotation,
            )
        except CoordinateMappingError as exc:
            raise SemanticTargetResolutionError(
                str(exc),
                error_code=exc.error_code,
                retryable=False,
            ) from exc
        self._validate_point(mapped.x, mapped.y)
        self._validate_safe_area(mapped.x, mapped.y)
        return mapped

    def _resolve_point(self, params: Mapping[str, Any], *, field_name: str) -> tuple[int, int]:
        mapped = self._resolve_mapped_point(params, field_name=field_name)
        return mapped.x, mapped.y

    def _validate_point(self, x: int, y: int) -> None:
        if not 0 <= x < self._device_w or not 0 <= y < self._device_h:
            raise SemanticTargetResolutionError(
                f"Android 坐标超出设备范围：({x}, {y}) / {self._device_w}x{self._device_h}",
                error_code="ANDROID_POINT_OUT_OF_BOUNDS",
                retryable=False,
            )

    def _validate_safe_area(self, x: int, y: int) -> None:
        try:
            assert_device_point_in_safe_area(
                x,
                y,
                device_w=self._device_w,
                device_h=self._device_h,
                safe_area=self._safe_area,
            )
        except CoordinateMappingError as exc:
            raise SemanticTargetResolutionError(
                str(exc),
                error_code=exc.error_code,
                retryable=False,
            ) from exc

    def _resolve_scroll(self, params: Mapping[str, Any]) -> ResolvedAndroidPointTarget:
        if isinstance(params.get("from"), Mapping) and isinstance(params.get("to"), Mapping):
            display_raw = params.get("display")
            display = display_raw if isinstance(display_raw, Mapping) else {}
            # 把 display 基准塞进 from/to 解析共用的 params 视图
            scroll_params = {
                **dict(params),
                "base_width": int(
                    display.get("width") or params.get("base_width") or CANONICAL_WIDTH
                ),
                "base_height": int(
                    display.get("height") or params.get("base_height") or CANONICAL_HEIGHT
                ),
            }

            def _pt(raw: Mapping[str, Any]) -> MappedDevicePoint:
                return self._resolve_mapped_point(
                    {
                        **scroll_params,
                        "point": raw,
                        "base_width": scroll_params["base_width"],
                        "base_height": scroll_params["base_height"],
                    },
                    field_name="point",
                )

            try:
                from_mapped = _pt(params["from"])
                to_mapped = _pt(params["to"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SemanticTargetResolutionError(
                    "scroll from/to 座標無效",
                    error_code="ANDROID_SCROLL_INVALID",
                    retryable=False,
                ) from exc
            ms = int(params.get("duration_ms") or 350)
            return ResolvedAndroidPointTarget(
                operation=InteractionOperation.SCROLL,
                swipe_x1=from_mapped.x,
                swipe_y1=from_mapped.y,
                swipe_x2=to_mapped.x,
                swipe_y2=to_mapped.y,
                swipe_ms=max(50, min(ms, 3_000)),
                resolution_method="parameters.scroll_bounds",
                mapping_audit=_mapping_audit_from_point(from_mapped),
            )
        direction = str(params.get("direction") or "down").lower()
        cx, cy = self._device_w // 2, self._device_h // 2
        dist = int(self._device_h * 0.28)
        if direction == "up":
            x1, y1, x2, y2 = cx, cy - dist // 2, cx, cy + dist // 2
        elif direction == "left":
            x1, y1, x2, y2 = cx + dist // 2, cy, cx - dist // 2, cy
        elif direction == "right":
            x1, y1, x2, y2 = cx - dist // 2, cy, cx + dist // 2, cy
        else:
            x1, y1, x2, y2 = cx, cy + dist // 2, cx, cy - dist // 2
        return ResolvedAndroidPointTarget(
            operation=InteractionOperation.SCROLL,
            swipe_x1=x1,
            swipe_y1=y1,
            swipe_x2=x2,
            swipe_y2=y2,
            swipe_ms=350,
            resolution_method=f"parameters.scroll_direction:{direction}",
        )


def _normalized_semantic_target(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _profile_credential_uia_label(params: Mapping[str, Any]) -> str | None:
    """账号凭证保存／我已保存：强制用短标签走 UIA，避开偏高合同框。"""

    if params.get("profile_credential_dismiss") is True or isinstance(
        params.get("app_profile_credential_dismiss"), Mapping
    ):
        return "我已保存"
    if params.get("profile_credential_save") is True or isinstance(
        params.get("app_profile_credential_save"), Mapping
    ):
        return "保存凭证"
    return None


def _point_inside_device(x: int, y: int, *, width: int, height: int) -> bool:
    return 0 <= x < width and 0 <= y < height


def _params_have_mappable_point(params: Mapping[str, Any]) -> bool:
    """parameters 是否已含可映射的 point（含 normalize 后的 resolved_target）。"""

    point = params.get("point")
    if isinstance(point, Mapping) and "x" in point and "y" in point:
        return True
    if "x" in params and "y" in params:
        return True
    return False


def _is_system_dialog_safe_alias_request(
    *,
    semantic_target: str,
    parameters: Mapping[str, Any],
) -> bool:
    """仅为系统弹窗中的安全延后/拒绝意图启用语义别名。"""

    if parameters.get("surface_phase_key") != "system_dialog":
        return False
    normalized = semantic_target.casefold()
    return _contains_marker(
        normalized,
        _SYSTEM_DIALOG_SAFE_MARKERS,
    ) and not _contains_unnegated_authorization_marker(normalized)


def _find_unique_system_dialog_safe_node(
    observation: AndroidRuntimeObservation,
):
    """从当前 UI Tree 选择唯一安全语义节点，拒绝授权候选与猜测。"""

    if not observation.available:
        return None
    matches = []
    for node in observation.nodes:
        if not (node.visible_to_user and node.enabled and node.clickable):
            continue
        label = " ".join(
            value.strip()
            for value in (node.text, node.content_desc)
            if value and value.strip()
        ).casefold()
        if (
            _contains_marker(label, _SYSTEM_DIALOG_SAFE_MARKERS)
            and not _contains_unnegated_authorization_marker(label)
        ):
            matches.append(node)
    if len(matches) > 1:
        raise SemanticTargetResolutionError(
            "Android 系统弹窗存在多个安全语义按钮，无法猜测",
            error_code="ANDROID_TARGET_AMBIGUOUS",
            retryable=False,
        )
    return matches[0] if matches else None


def _popup_close_control_aliases(
    *,
    semantic_target: str,
    parameters: Mapping[str, Any],
) -> frozenset[str]:
    """只为阶段机已确认的关闭动作生成有限控件别名。"""

    if (
        parameters.get("surface_phase_key") != "popup_chain"
        or parameters.get("decision_status") != "ready_to_close"
        or parameters.get("reason_code") != "close_control_ready"
    ):
        return frozenset()
    normalized = semantic_target.casefold()
    if not _contains_marker(normalized, _POPUP_CLOSE_CONTEXT_MARKERS):
        return frozenset()
    aliases: set[str] = set()
    if "确认" in normalized or "公告" in normalized:
        aliases.update({"确认", "确定", "ok", "confirm"})
    if "关闭" in normalized or "close" in normalized or "dismiss" in normalized:
        aliases.update({"关闭", "close", "dismiss", "×"})
    if "取消" in normalized or "cancel" in normalized:
        aliases.update({"取消", "cancel"})
    if "知道" in normalized:
        aliases.update({"知道了", "我知道了", "got it"})
    return frozenset(item.casefold() for item in aliases)


def _find_unique_popup_close_node(
    observation: AndroidRuntimeObservation,
    *,
    aliases: frozenset[str],
):
    """用当前 Accessibility 的唯一关闭控件覆盖易漂移的视觉坐标。"""

    if not observation.available:
        return None
    matches = []
    for node in observation.nodes:
        if not (node.visible_to_user and node.enabled and node.clickable):
            continue
        # 铺满整屏的关闭语义节点是可点穿蒙层，取中心会点到广告本体
        if _is_full_surface_bounds(
            node.bounds,
            surface_width=observation.width,
            surface_height=observation.height,
        ):
            continue
        labels = {
            value.strip().casefold()
            for value in (node.text, node.content_desc)
            if value and value.strip()
        }
        if labels & aliases:
            matches.append(node)
    if len(matches) > 1:
        raise SemanticTargetResolutionError(
            "Android 弹窗存在多个关闭语义按钮，无法猜测",
            error_code="ANDROID_TARGET_AMBIGUOUS",
            retryable=False,
        )
    return matches[0] if matches else None


def _is_full_surface_bounds(
    bounds: AndroidBounds,
    *,
    surface_width: int,
    surface_height: int,
) -> bool:
    """判断控件边界是否铺满整屏（含近似整屏的大面积蒙层）。"""

    if surface_width <= 0 or surface_height <= 0:
        return False
    spans_both_axes = (
        bounds.width >= surface_width * _FULL_SURFACE_AXIS_RATIO
        and bounds.height >= surface_height * _FULL_SURFACE_AXIS_RATIO
    )
    area_ratio = (bounds.width * bounds.height) / float(
        surface_width * surface_height
    )
    return spans_both_axes or area_ratio >= _FULL_SURFACE_AREA_RATIO


def _contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker.casefold() in text for marker in markers)


def _contains_unnegated_authorization_marker(text: str) -> bool:
    """忽略同一语义分句中被否定的授权词，如“不允许”。"""

    for marker in _SYSTEM_DIALOG_AUTHORIZATION_MARKERS:
        normalized_marker = marker.casefold()
        search_from = 0
        while True:
            marker_start = text.find(normalized_marker, search_from)
            if marker_start < 0:
                break
            prefix = _authorization_clause_prefix(text, marker_start).rstrip()
            directly_negated = prefix.endswith(("不", "not"))
            explicitly_negated = any(
                negation.casefold() in prefix
                for negation in _SYSTEM_DIALOG_AUTHORIZATION_NEGATIONS
            )
            if not directly_negated and not explicitly_negated:
                return True
            search_from = marker_start + len(normalized_marker)
    return False


def _authorization_clause_prefix(text: str, marker_start: int) -> str:
    """返回授权词前、且不跨越并列或标点分隔符的语义分句。"""

    prefix = text[:marker_start]
    separator_start = max(
        (prefix.rfind(separator), separator)
        for separator in _SYSTEM_DIALOG_AUTHORIZATION_CLAUSE_SEPARATORS
    )
    if separator_start[0] < 0:
        return prefix
    return prefix[separator_start[0] + len(separator_start[1]) :]


# Vision 长意图里常见的动作前缀；较长者优先，剥掉后用于短标签精确命中。
_SEMANTIC_ACTION_PREFIXES = (
    "点击进入",
    "點擊進入",
    "点击",
    "點擊",
    "打开",
    "打開",
    "进入",
    "進入",
    "选择",
    "選擇",
    "关闭",
    "關閉",
    "前往",
    "查看",
)
_MIN_CONTAINED_LABEL_LEN = 2


def _semantic_label_candidates(semantic_target: str) -> tuple[str, ...]:
    """从 Vision／意图文案抽出可与 UIA text／content-desc 对齐的标签变体。"""

    text = " ".join(semantic_target.split()).strip()
    if not text:
        return ()
    out: list[str] = []

    def add(value: str) -> None:
        cleaned = " ".join(value.split()).strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)

    add(text)
    for left, right in (
        ("「", "」"),
        ("『", "』"),
        ("[", "]"),
        ("“", "”"),
        ('"', '"'),
        ("'", "'"),
    ):
        start = text.find(left)
        end = text.find(right, start + 1) if start >= 0 else -1
        if start >= 0 and end > start + 1:
            add(text[start + 1 : end])
    for prefix in _SEMANTIC_ACTION_PREFIXES:
        if text.startswith(prefix) and len(text) > len(prefix):
            add(text[len(prefix) :].lstrip(" ：:，,。"))
    return tuple(out)


def _node_semantic_labels(node: Any) -> tuple[str, ...]:
    labels: list[str] = []
    for value in (node.text, node.content_desc):
        if isinstance(value, str) and value.strip():
            cleaned = value.strip()
            if cleaned not in labels:
                labels.append(cleaned)
    return tuple(labels)


def _clickable_non_fullscreen_nodes(observation: AndroidRuntimeObservation):
    """可见、可用、可点且非整屏蒙层的节点。"""

    return [
        node
        for node in observation.nodes
        if node.visible_to_user
        and node.enabled
        and node.clickable
        and not _is_full_surface_bounds(
            node.bounds,
            surface_width=observation.width,
            surface_height=observation.height,
        )
    ]


def _bounds_identity(bounds: AndroidBounds) -> tuple[int, int, int, int]:
    return (bounds.left, bounds.top, bounds.right, bounds.bottom)


def _find_exact_clickable_node(
    observation: AndroidRuntimeObservation,
    *,
    semantic_target: str,
):
    """同帧 UIA 唯一语义命中：精确标签变体优先，其次最长唯一包含。

    非底栏业务点常是 Vision 长意图（如「点击「动态」进入详情」）对 UIA 短
    text／content-desc。有唯一结构命中时优先设备 bounds 中心，覆盖 Vision
    ``capture_px``；多命中 fail-closed；无命中返回 ``None`` 回退视觉 point。
    """

    if not observation.available:
        return None
    label_candidates = _semantic_label_candidates(semantic_target)
    if not label_candidates:
        return None
    nodes = _clickable_non_fullscreen_nodes(observation)

    for label in label_candidates:
        matches = [
            node
            for node in nodes
            if label in _node_semantic_labels(node)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise SemanticTargetResolutionError(
                f"Android 语义目标存在多个精确可点击节点：{label}",
                error_code="ANDROID_TARGET_AMBIGUOUS",
                retryable=False,
            )

    # 唯一包含：节点短标签出现在意图文案中；多节点取最长，同长多节点拒绝。
    scored: list[tuple[int, tuple[int, int, int, int], Any]] = []
    for node in nodes:
        best_len = 0
        for field in _node_semantic_labels(node):
            if len(field) < _MIN_CONTAINED_LABEL_LEN:
                continue
            if any(field == hay or field in hay for hay in label_candidates):
                best_len = max(best_len, len(field))
        if best_len > 0:
            scored.append((best_len, _bounds_identity(node.bounds), node))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_len = scored[0][0]
    unique: list[Any] = []
    seen: set[tuple[int, int, int, int]] = set()
    for length, key, node in scored:
        if length < best_len:
            break
        if key in seen:
            continue
        seen.add(key)
        unique.append(node)
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        raise SemanticTargetResolutionError(
            f"Android 语义目标存在多个可点击节点：{semantic_target}",
            error_code="ANDROID_TARGET_AMBIGUOUS",
            retryable=False,
        )
    return None


def _uia_semantic_resolution_method(node: Any, *, semantic_target: str) -> str:
    """区分精确 text／content-desc 命中与唯一包含校准。"""

    labels = set(_semantic_label_candidates(semantic_target))
    labels.add(semantic_target.strip())
    if node.content_desc and node.content_desc in labels:
        return "uiautomator.content_desc"
    if node.text and node.text in labels:
        return "uiautomator.text"
    return "uiautomator.semantic_unique"


def _center_of_bounds(bounds: AndroidBounds) -> tuple[int, int]:
    return (
        bounds.left + bounds.width // 2,
        bounds.top + bounds.height // 2,
    )


def _point_resolution_method(params: Mapping[str, Any]) -> str:
    if isinstance(params.get("resolved_target"), Mapping):
        return "parameters.resolved_target"
    if isinstance(params.get("point"), Mapping):
        return "parameters.point"
    if "x" in params and "y" in params:
        return "parameters.x_y"
    return "parameters.unknown_point"
