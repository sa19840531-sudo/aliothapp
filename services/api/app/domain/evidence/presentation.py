"""证据展示层评估：一份采集材料"当前最上层是谁、底层页面是否可见、是否受诊断操作影响"。

对应专项七《可信证据留存》§5.2/§5.5 与实现级设计 §8.6/§18.7：
可见层是正式门禁而不是提示；诊断操作（隐藏弹窗、脚本派发点击等）产生的材料
带标记并沿同一会话向后传播，不能冒充正常用户操作证据。

本模块只定义不可变值对象与消费规则；平台推导（H5 UI Snapshot、App 视觉观察）
在 application 层完成，写入口与消费口共用同一份合同。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

PRESENTATION_PROTOCOL_VERSION = "presentation-stack.v1"

# 采集元数据里承载展示层评估的键；qc_page_capture 与 qc_capture 通过触发器同步。
PRESENTATION_METADATA_KEY = "presentation"


class TopmostLayerKind(StrEnum):
    """当前画面最上层内容的种类。"""

    PAGE = "page"
    OVERLAY = "overlay"
    SYSTEM_DIALOG = "system_dialog"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


class BasePageVisibility(StrEnum):
    """底层业务页面在这一帧的可见程度。"""

    VISIBLE = "visible"
    PARTIALLY_OCCLUDED = "partially_occluded"
    FULLY_OCCLUDED = "fully_occluded"
    UNKNOWN = "unknown"


class BlockingScope(StrEnum):
    """最上层内容阻断交互的范围。"""

    NONE = "none"
    REGION = "region"
    PAGE = "page"
    APPLICATION = "application"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class MutationMode(StrEnum):
    """材料是否来自被平台改动过的页面。"""

    NATURAL = "natural"
    INSTRUMENTED_DIAGNOSTIC = "instrumented_diagnostic"


class EvidenceLayerTarget(StrEnum):
    """规则要求证据落在哪一层。"""

    BASE_PAGE = "base_page"
    OVERLAY = "overlay"
    ANY = "any"


class PresentationRejectionCode(StrEnum):
    """展示层门禁拒绝材料进入某个证据角色的原因码。"""

    BASE_PAGE_OCCLUDED = "BASE_PAGE_OCCLUDED"
    DIAGNOSTIC_EVIDENCE_EXCLUDED = "DIAGNOSTIC_EVIDENCE_EXCLUDED"
    EXTERNAL_SURFACE = "EXTERNAL_SURFACE"


_OCCLUDING_LAYERS = frozenset({TopmostLayerKind.OVERLAY, TopmostLayerKind.SYSTEM_DIALOG})
_UNEXPOSED_VISIBILITY = frozenset(
    {BasePageVisibility.FULLY_OCCLUDED, BasePageVisibility.UNKNOWN}
)
_LAYER_LABELS = {
    TopmostLayerKind.OVERLAY: "弹窗或浮层",
    TopmostLayerKind.SYSTEM_DIALOG: "系统对话框",
    TopmostLayerKind.EXTERNAL: "外部页面",
}
_DIAGNOSTIC_LABELS = {
    "style_hide_obstruction": "用样式隐藏弹窗",
    "js_click_dispatch": "脚本派发点击",
    "dom_mutation": "改动页面结构",
}


@dataclass(frozen=True, slots=True)
class DiagnosticOperation:
    """一次改动过页面的诊断操作及其来源。"""

    kind: str
    source: str
    # None 表示本采集自身发生的操作；否则为同一会话中更早采集的 ID（继承而来）。
    origin_capture_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind, "source": self.source}
        if self.origin_capture_id is not None:
            payload["origin_capture_id"] = self.origin_capture_id
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> DiagnosticOperation | None:
        if not isinstance(value, Mapping):
            return None
        kind = _text(value.get("kind"))
        if not kind:
            return None
        return cls(
            kind=kind,
            source=_text(value.get("source")) or kind,
            origin_capture_id=_text(value.get("origin_capture_id")) or None,
        )

    def inherited_by(self, *, origin_capture_id: str) -> DiagnosticOperation:
        """把自身发生的操作改写为"继承自某次采集"，用于向后传播。"""
        if self.origin_capture_id is not None:
            return self
        return replace(self, origin_capture_id=origin_capture_id)


@dataclass(frozen=True, slots=True)
class PresentationAssessment:
    """一次采集的展示层评估（不可变、可持久化、可重建）。"""

    topmost_layer_kind: TopmostLayerKind
    base_page_visibility: BasePageVisibility
    blocking_scope: BlockingScope
    mutation_mode: MutationMode = MutationMode.NATURAL
    overlay_signature: str | None = None
    overlay_semantic_kind: str | None = None
    overlay_coverage: float | None = None
    hit_test_status: str = "unknown"
    evidence_sources: tuple[str, ...] = ()
    diagnostic_operations: tuple[DiagnosticOperation, ...] = ()
    uncertainty: tuple[str, ...] = ()
    protocol_version: str = PRESENTATION_PROTOCOL_VERSION
    # 采集所在文档的起始时间（performance.timeOrigin，毫秒）：整页重新加载才会变化，
    # 页内跳转、切 Tab、开弹层都不变。诊断标记只在同一文档内向后继承。
    document_epoch_ms: float | None = None

    def __post_init__(self) -> None:
        if self.overlay_coverage is not None and not 0.0 <= self.overlay_coverage <= 1.0:
            raise ValueError("overlay_coverage 必须在 0 与 1 之间")
        if (
            self.mutation_mode is MutationMode.INSTRUMENTED_DIAGNOSTIC
            and not self.diagnostic_operations
        ):
            raise ValueError("诊断模式必须记录至少一项诊断操作")
        if self.mutation_mode is MutationMode.NATURAL and self.diagnostic_operations:
            raise ValueError("自然模式不得携带诊断操作记录")

    @property
    def base_page_exposed(self) -> bool:
        """底层页面是否有可见区域可作为该页面的证据。"""
        return self.base_page_visibility in {
            BasePageVisibility.VISIBLE,
            BasePageVisibility.PARTIALLY_OCCLUDED,
        }

    @property
    def is_diagnostic(self) -> bool:
        return self.mutation_mode is MutationMode.INSTRUMENTED_DIAGNOSTIC

    @property
    def occluding_layer_present(self) -> bool:
        return self.topmost_layer_kind in _OCCLUDING_LAYERS

    def with_diagnostic_operations(
        self, operations: Sequence[DiagnosticOperation]
    ) -> PresentationAssessment:
        """叠加诊断操作（自身或继承），结果恒为诊断模式。"""
        merged = _unique_operations((*self.diagnostic_operations, *operations))
        if not merged:
            return self
        return replace(
            self,
            mutation_mode=MutationMode.INSTRUMENTED_DIAGNOSTIC,
            diagnostic_operations=merged,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "topmost_layer_kind": self.topmost_layer_kind.value,
            "base_page_visibility": self.base_page_visibility.value,
            "blocking_scope": self.blocking_scope.value,
            "mutation_mode": self.mutation_mode.value,
            "hit_test_status": self.hit_test_status,
            "evidence_sources": list(self.evidence_sources),
            "diagnostic_operations": [item.to_dict() for item in self.diagnostic_operations],
            "uncertainty": list(self.uncertainty),
        }
        if self.overlay_signature is not None:
            payload["overlay_signature"] = self.overlay_signature
        if self.overlay_semantic_kind is not None:
            payload["overlay_semantic_kind"] = self.overlay_semantic_kind
        if self.overlay_coverage is not None:
            payload["overlay_coverage"] = round(self.overlay_coverage, 4)
        if self.document_epoch_ms is not None:
            payload["document_epoch_ms"] = self.document_epoch_ms
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> PresentationAssessment | None:
        """宽松读取：缺字段退到 unknown，非法结构返回 None，不抛给消费者。"""
        if not isinstance(value, Mapping):
            return None
        if _text(value.get("protocol_version")) != PRESENTATION_PROTOCOL_VERSION:
            return None
        operations = _unique_operations(
            tuple(
                item
                for item in (
                    DiagnosticOperation.from_dict(raw)
                    for raw in _sequence(value.get("diagnostic_operations"))
                )
                if item is not None
            )
        )
        mutation_mode = _enum(MutationMode, value.get("mutation_mode"), MutationMode.NATURAL)
        if operations and mutation_mode is MutationMode.NATURAL:
            mutation_mode = MutationMode.INSTRUMENTED_DIAGNOSTIC
        if mutation_mode is MutationMode.INSTRUMENTED_DIAGNOSTIC and not operations:
            operations = (DiagnosticOperation(kind="unknown", source="unrecorded"),)
        return cls(
            topmost_layer_kind=_enum(
                TopmostLayerKind, value.get("topmost_layer_kind"), TopmostLayerKind.UNKNOWN
            ),
            base_page_visibility=_enum(
                BasePageVisibility,
                value.get("base_page_visibility"),
                BasePageVisibility.UNKNOWN,
            ),
            blocking_scope=_enum(
                BlockingScope, value.get("blocking_scope"), BlockingScope.UNKNOWN
            ),
            mutation_mode=mutation_mode,
            overlay_signature=_text(value.get("overlay_signature")) or None,
            overlay_semantic_kind=_text(value.get("overlay_semantic_kind")) or None,
            overlay_coverage=_bounded_ratio(value.get("overlay_coverage")),
            hit_test_status=_text(value.get("hit_test_status")) or "unknown",
            evidence_sources=_text_tuple(value.get("evidence_sources")),
            diagnostic_operations=operations,
            uncertainty=_text_tuple(value.get("uncertainty")),
            document_epoch_ms=_epoch_ms(value.get("document_epoch_ms")),
        )


def unknown_presentation(
    *,
    uncertainty: Sequence[str] = ("presentation_unavailable",),
    evidence_sources: Sequence[str] = (),
) -> PresentationAssessment:
    """没有任何层级事实时的保守评估：不宣称可见，也不宣称被遮挡。"""
    return PresentationAssessment(
        topmost_layer_kind=TopmostLayerKind.UNKNOWN,
        base_page_visibility=BasePageVisibility.UNKNOWN,
        blocking_scope=BlockingScope.UNKNOWN,
        evidence_sources=tuple(evidence_sources),
        uncertainty=tuple(dict.fromkeys(str(item) for item in uncertainty)),
    )


@dataclass(frozen=True, slots=True)
class PresentationVerdict:
    """展示层门禁对"某材料能否进入某规则的证据角色"的结论。"""

    accepted: bool
    layer_target: EvidenceLayerTarget
    reason_code: PresentationRejectionCode | None = None
    reason: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "layer_target": self.layer_target.value,
            "reason_code": self.reason_code.value if self.reason_code else None,
            "reason": self.reason,
            "facts": dict(self.facts),
        }


def evaluate_presentation_for_rule(
    presentation: PresentationAssessment | None,
    *,
    layer_target: EvidenceLayerTarget,
    accepts_diagnostic: bool = False,
) -> PresentationVerdict:
    """按规则要求的证据层裁决一份材料。

    - 缺少评估时不拒绝：拒绝必须有"最上层确实是遮挡物"或"确有诊断操作"的正向依据。
    - 诊断材料对所有未声明接受诊断证据的规则一律拒绝（§5.5）。
    - 规则要求底层页面可见（base_page）而最上层是弹窗/系统对话框且底层不可见时拒绝（§5.2）。
    """
    if presentation is None:
        return PresentationVerdict(accepted=True, layer_target=layer_target)
    facts = {
        "topmost_layer_kind": presentation.topmost_layer_kind.value,
        "base_page_visibility": presentation.base_page_visibility.value,
        "blocking_scope": presentation.blocking_scope.value,
        "mutation_mode": presentation.mutation_mode.value,
        "overlay_signature": presentation.overlay_signature,
        "overlay_semantic_kind": presentation.overlay_semantic_kind,
    }
    if presentation.is_diagnostic and not accepts_diagnostic:
        kinds = "、".join(
            dict.fromkeys(
                _DIAGNOSTIC_LABELS.get(item.kind, item.kind)
                for item in presentation.diagnostic_operations
            )
        )
        return PresentationVerdict(
            accepted=False,
            layer_target=layer_target,
            reason_code=PresentationRejectionCode.DIAGNOSTIC_EVIDENCE_EXCLUDED,
            reason=(
                f"这份材料来自平台诊断操作（{kinds}）之后的页面，"
                "不能当作正常用户操作下的证据"
            ),
            facts=facts,
        )
    if layer_target is not EvidenceLayerTarget.BASE_PAGE:
        return PresentationVerdict(accepted=True, layer_target=layer_target, facts=facts)
    if presentation.topmost_layer_kind is TopmostLayerKind.EXTERNAL:
        return PresentationVerdict(
            accepted=False,
            layer_target=layer_target,
            reason_code=PresentationRejectionCode.EXTERNAL_SURFACE,
            reason="当前画面是外部页面，不是被测业务页面，不能作为该页面的证据",
            facts=facts,
        )
    if (
        presentation.occluding_layer_present
        and presentation.base_page_visibility in _UNEXPOSED_VISIBILITY
    ):
        label = _LAYER_LABELS.get(presentation.topmost_layer_kind, "遮挡层")
        signature = (
            f"（标识 {presentation.overlay_signature}）" if presentation.overlay_signature else ""
        )
        return PresentationVerdict(
            accepted=False,
            layer_target=layer_target,
            reason_code=PresentationRejectionCode.BASE_PAGE_OCCLUDED,
            reason=(
                f"当前画面最上层是{label}{signature}，底层页面被遮挡，"
                "截图和页面结构里的底层内容不能当作这个页面的可见证据；"
                "需要关闭遮挡后的页面证据"
            ),
            facts=facts,
        )
    return PresentationVerdict(accepted=True, layer_target=layer_target, facts=facts)


def _unique_operations(
    operations: Sequence[DiagnosticOperation],
) -> tuple[DiagnosticOperation, ...]:
    seen: dict[tuple[str, str, str | None], DiagnosticOperation] = {}
    for item in operations:
        key = (item.kind, item.source, item.origin_capture_id)
        seen.setdefault(key, item)
    return tuple(seen.values())


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _text_tuple(value: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_text(item) for item in _sequence(value) if _text(item)))


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, (list, tuple)):
        return value
    return ()


def _enum(kind: type[Any], value: Any, default: Any) -> Any:
    try:
        return kind(_text(value))
    except ValueError:
        return default


def _bounded_ratio(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return None
    if ratio != ratio:  # NaN
        return None
    return min(max(ratio, 0.0), 1.0)


def _epoch_ms(value: Any) -> float | None:
    """文档起始时间：只接受有限正数，其余（缺失、布尔、非数值）一律 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        return None
    if epoch != epoch or epoch in (float("inf"), float("-inf")) or epoch <= 0:
        return None
    return epoch
