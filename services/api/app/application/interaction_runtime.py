"""Interaction Runtime 的内部语义解析与动作证据暂存端口。"""

from __future__ import annotations

import asyncio
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from services.api.app.domain.exploration.h5_logical_surface import (
    stable_h5_surface_hash,
)
from services.api.app.domain.exploration.h5_visual_navigation import (
    VisualNavigationCapability,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionResult,
)

H5_DYNAMIC_LOGICAL_TARGET_MODES = frozenset(
    {
        "bottom_navigation",
        "list_detail_navigation",
        "page_tab_navigation",
        "profile_activity_navigation",
        "profile_payment_navigation",
        "profile_settings_navigation",
        "profile_favorites_page_back",
        "settings_page_back",
    }
)


class SemanticTargetResolutionError(RuntimeError):
    """语义目标无法唯一、安全地映射到当前平台目标。"""

    def __init__(self, message: str, *, error_code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


@dataclass(frozen=True, kw_only=True)
class SemanticTargetResolutionRequest:
    """只在 Interaction Runtime 内部使用的目标解析请求。"""

    task_id: str
    session_id: str
    source_capture_id: str
    action_type: InteractionOperation
    semantic_target: str | None
    parameters: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class VisualNavigationRuntimeTarget:
    """只在进程内传播的视觉导航来源；repr 不暴露敏感材料。"""

    capability: VisualNavigationCapability = field(repr=False)
    source_png: bytes = field(repr=False)
    source_image_sha256: str = field(repr=False)


@dataclass(frozen=True, kw_only=True)
class LogicalTargetRuntimeBinding:
    """仅在 Runtime 内传播的动作前逻辑页面绑定。"""

    source_identity_fingerprint: str
    source_state_fingerprint: str
    target_signature: str
    target_source: str | None = None
    target_identity_signature: str | None = None
    allow_dynamic_state_revalidation: bool = False
    allow_dynamic_geometry_revalidation: bool = False
    allow_horizontal_offscreen_revalidation: bool = False


def logical_target_snapshot_signature(
    element: Mapping[str, Any],
    *,
    target_source: str,
) -> str | None:
    """按 Runtime 绑定合同计算 UI Snapshot 目标结构签名。"""

    rect = _logical_target_rect(element)
    if rect is None:
        return None
    label = ""
    for key in ("text", "accessible_name", "aria_label", "ocr_text"):
        label = _normalized_target_text(element.get(key))
        if label:
            break
    return stable_h5_surface_hash(
        {
            **_logical_target_identity_payload(
                element,
                target_source=target_source,
                label=label,
            ),
            "rect_bucket": {key: round(value / 8) for key, value in rect.items()},
        }
    )


def logical_target_snapshot_identity_signature(
    element: Mapping[str, Any],
    *,
    target_source: str,
) -> str | None:
    """计算不含瞬时布局坐标的 H5 目标身份签名。"""

    selector = str(element.get("selector") or "").strip()
    if not selector:
        return None
    label = ""
    for key in ("text", "accessible_name", "aria_label", "ocr_text"):
        label = _normalized_target_text(element.get(key))
        if label:
            break
    return stable_h5_surface_hash(
        {
            **_logical_target_identity_payload(
                element,
                target_source=target_source,
                label=label,
            ),
            "selector": selector,
        }
    )


def _logical_target_identity_payload(
    element: Mapping[str, Any],
    *,
    target_source: str,
    label: str,
) -> dict[str, str | None]:
    return {
        "source": target_source,
        "role": _normalized_target_text(element.get("role")),
        "label": label,
        "parent": _logical_target_parent_signature(element),
    }


def _logical_target_rect(element: Mapping[str, Any]) -> dict[str, float] | None:
    raw = element.get("rect")
    if not isinstance(raw, Mapping):
        return None
    values: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        value = raw.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(float(value))
        ):
            return None
        values[key] = float(value)
    if values["width"] <= 0 or values["height"] <= 0:
        return None
    return values


def _normalized_target_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def _logical_target_parent_signature(element: Mapping[str, Any]) -> str | None:
    for key in ("parent_selector", "parent_semantics_node_id", "semantics_path"):
        value = element.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()[:500]
    ancestors = element.get("ancestor_class_tokens")
    if isinstance(ancestors, list):
        values = sorted(
            {
                str(value).strip().casefold()
                for value in ancestors
                if str(value).strip()
            }
        )
        return " ".join(values)[:500] or None
    return None


@dataclass(frozen=True, kw_only=True)
class ResolvedInteractionTarget:
    """平台内部动作绑定；禁止序列化或返回给 Agent。"""

    selector: str | None = None
    submit_selector: str | None = None
    destination_url: str | None = None
    input_text: str | None = None
    fault_url_pattern: str | None = None
    clear_obstructions: bool = False
    allow_high_z_obstruction_hide: bool = False
    h5_popup_dismissal_strategy: str | None = None
    # 从快照叶节点激活已验签的业务卡片容器；模式只允许执行端维护的固定集合。
    verified_card_activation: str | None = None
    # 多匹配 selector 消歧：解析时选中元素的快照矩形；执行端在多个命中里
    # 优先点击与该几何最接近的候选，避免命中同路径的容器级元素。
    expected_rect: Mapping[str, float] | None = None
    click_x_ratio: float | None = None
    click_y_ratio: float | None = None
    left_ratio: float | None = None
    top_ratio: float | None = None
    right_ratio: float | None = None
    bottom_ratio: float | None = None
    visual_confidence: float | None = None
    visual_source_url: str | None = None
    visual_entry_kind: str | None = None
    visual_expected_text: str | None = None
    screenshot_index: int | None = None
    # 第三阶段逻辑目标门禁的审计绑定；仅在显式开启且来源状态验签通过时赋值。
    target_source: str | None = None
    target_signature: str | None = None
    target_identity_signature: str | None = None
    source_state_fingerprint: str | None = None
    target_trust: str | None = None
    # 由已保存 UI Snapshot/视觉候选解析出的控件语义，供执行前风险复核；
    # 不包含 selector、句柄或可让 Agent 复用的底层定位材料。
    trusted_target_texts: tuple[str, ...] = ()
    trusted_target_role: str | None = None
    visual_navigation_target: VisualNavigationRuntimeTarget | None = field(
        default=None,
        repr=False,
    )


class SemanticTargetResolver(Protocol):
    """把业务语义目标解析为平台内部绑定。"""

    async def resolve(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget: ...


class TrustedUiSnapshotReader(Protocol):
    """按任务与 Capture 归属读取已校验的 UI Snapshot。"""

    async def read_ui_snapshot(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> Mapping[str, Any]: ...

    async def read_web_before_action_binding(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> Mapping[str, Any]: ...

    async def read_visual_evidence_checksum(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        artifact_id: str,
        frame_index: int | None,
    ) -> str: ...

    async def read_visual_evidence_image(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        artifact_id: str,
        expected_sha256: str,
    ) -> bytes: ...

    async def read_visual_evidence_region_checksum(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        artifact_id: str | None,
        left_ratio: float,
        top_ratio: float,
        right_ratio: float,
        bottom_ratio: float,
    ) -> str: ...


@dataclass(frozen=True, kw_only=True)
class PendingActionEvidence:
    """动作完成后、后 Capture 提交前的内部证据。"""

    interaction: InteractionResult
    browser_result: Any


class ActionEvidenceBuffer(Protocol):
    """连接动作执行与后 Capture 的进程内暂存端口。"""

    async def get(self, *, action_execution_id: str) -> PendingActionEvidence | None: ...

    async def put(self, evidence: PendingActionEvidence) -> None: ...

    async def discard(self, *, action_execution_id: str) -> None: ...


class InMemoryActionEvidenceBuffer:
    """M1 单进程实现；重复动作身份只复用已完成动作，不再次触发 Driver。"""

    def __init__(self, *, max_entries: int = 2_000) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries 必须大于 0")
        self._max_entries = max_entries
        self._items: dict[str, PendingActionEvidence] = {}
        self._lock = asyncio.Lock()

    async def get(self, *, action_execution_id: str) -> PendingActionEvidence | None:
        async with self._lock:
            return self._items.get(action_execution_id)

    async def put(self, evidence: PendingActionEvidence) -> None:
        action_execution_id = evidence.interaction.action_execution_id
        async with self._lock:
            existing = self._items.get(action_execution_id)
            if existing is not None:
                if existing.interaction.intent_id != evidence.interaction.intent_id:
                    raise RuntimeError("动作执行身份冲突")
                return
            if len(self._items) >= self._max_entries:
                raise RuntimeError("动作证据暂存已满，禁止继续执行新动作")
            self._items[action_execution_id] = evidence

    async def discard(self, *, action_execution_id: str) -> None:
        async with self._lock:
            self._items.pop(action_execution_id, None)
