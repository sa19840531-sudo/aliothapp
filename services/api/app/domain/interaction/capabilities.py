"""Surface Adapter 的版本化能力清单。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from services.api.app.domain.interaction.enums import SurfaceKind
from services.api.app.domain.interaction.validation import (
    InteractionValidationError,
    freeze_mapping,
    require_text,
)

CAPABILITY_MANIFEST_PROTOCOL_VERSION = "surface-capabilities.v1"


class CapabilityMismatchError(InteractionValidationError):
    """Adapter 未提供工作流要求的可用能力。"""


class CapabilityStatus(StrEnum):
    """能力交付成熟度。"""

    READY = "ready"
    PARTIAL = "partial"
    STUB = "stub"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, kw_only=True)
class CapabilityDescriptor:
    """单项动作、观察或产物能力。"""

    status: CapabilityStatus
    target_modes: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    notes: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, CapabilityStatus):
            raise InteractionValidationError("capability.status 不受支持")
        object.__setattr__(
            self,
            "target_modes",
            _validate_text_tuple(self.target_modes, "capability.target_modes"),
        )
        object.__setattr__(
            self,
            "fields",
            _validate_text_tuple(self.fields, "capability.fields"),
        )
        if self.notes is not None:
            require_text(self.notes, "capability.notes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "target_modes": list(self.target_modes),
            "fields": list(self.fields),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CapabilityDescriptor:
        try:
            status = CapabilityStatus(payload["status"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InteractionValidationError("capability.status 不受支持") from exc
        return cls(
            status=status,
            target_modes=_read_sequence(payload.get("target_modes", ()), "target_modes"),
            fields=_read_sequence(payload.get("fields", ()), "fields"),
            notes=payload.get("notes"),
        )


@dataclass(frozen=True, kw_only=True)
class SurfaceCapabilityManifest:
    """Adapter 可执行动作和可采集证据的版本化能力清单。"""

    adapter_id: str
    adapter_version: str
    surface_kind: SurfaceKind
    platform: str
    backends: tuple[str, ...]
    actions: Mapping[str, CapabilityDescriptor]
    observations: Mapping[str, CapabilityDescriptor]
    artifacts: Mapping[str, CapabilityDescriptor]
    features: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: str = CAPABILITY_MANIFEST_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_text(self.adapter_id, "adapter_id")
        require_text(self.adapter_version, "adapter_version")
        require_text(self.platform, "platform")
        if not isinstance(self.surface_kind, SurfaceKind):
            raise InteractionValidationError("surface_kind 不受支持")
        if self.protocol_version != CAPABILITY_MANIFEST_PROTOCOL_VERSION:
            raise InteractionValidationError("Capability Manifest protocol_version 不受支持")
        backends = _validate_text_tuple(self.backends, "backends")
        if not backends:
            raise InteractionValidationError("backends 不能为空")
        object.__setattr__(self, "backends", backends)
        for field_name in ("actions", "observations", "artifacts"):
            object.__setattr__(
                self,
                field_name,
                _freeze_descriptors(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "features", freeze_mapping(self.features, "features"))

    @property
    def adapter_name(self) -> str:
        """旧 Web 调用的只读兼容名称。"""
        return self.adapter_id

    @property
    def action_types(self) -> tuple[str, ...]:
        return _available_names(self.actions)

    @property
    def artifact_types(self) -> tuple[str, ...]:
        return _available_names(self.artifacts)

    def require_ready(self, capability_names: Sequence[str]) -> None:
        missing: list[str] = []
        for name in capability_names:
            descriptor = (
                self.actions.get(name)
                or self.observations.get(name)
                or self.artifacts.get(name)
            )
            if descriptor is None or descriptor.status is not CapabilityStatus.READY:
                missing.append(name)
        if missing:
            raise CapabilityMismatchError(
                "Adapter 缺少 ready 能力：" + ", ".join(sorted(set(missing)))
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "surface_kind": self.surface_kind.value,
            "platform": self.platform,
            "backends": list(self.backends),
            "actions": _descriptor_payload(self.actions),
            "observations": _descriptor_payload(self.observations),
            "artifacts": _descriptor_payload(self.artifacts),
            "features": dict(self.features),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SurfaceCapabilityManifest:
        try:
            surface_kind = SurfaceKind(payload["surface_kind"])
            protocol_version = payload["protocol_version"]
            adapter_id = payload["adapter_id"]
            adapter_version = payload["adapter_version"]
            platform = payload["platform"]
            backends = _read_sequence(payload["backends"], "backends")
        except (KeyError, TypeError, ValueError) as exc:
            raise InteractionValidationError("Capability Manifest 必填字段不合法") from exc
        return cls(
            protocol_version=protocol_version,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            surface_kind=surface_kind,
            platform=platform,
            backends=backends,
            actions=_read_descriptors(payload.get("actions"), "actions"),
            observations=_read_descriptors(payload.get("observations"), "observations"),
            artifacts=_read_descriptors(payload.get("artifacts"), "artifacts"),
            features=_read_mapping(payload.get("features", {}), "features"),
        )


def build_android_screenshot_only_manifest(
    *,
    adapter_id: str,
    adapter_version: str,
    backend: str,
) -> SurfaceCapabilityManifest:
    """构造 App 一期最小生产能力，缺失能力必须显式 unsupported。"""
    ready = CapabilityDescriptor(status=CapabilityStatus.READY)
    unsupported = CapabilityDescriptor(status=CapabilityStatus.UNSUPPORTED)
    media_state = CapabilityDescriptor(
        status=CapabilityStatus.PARTIAL,
        fields=("kind", "bounds", "playback_state_available", "limitations"),
        notes="Accessibility 可证明媒体控件候选，播放时长、进度和资源 URL 不可用",
    )
    return SurfaceCapabilityManifest(
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        surface_kind=SurfaceKind.ANDROID,
        platform="android",
        backends=(backend,),
        actions={
            "session": ready,
            "install": ready,
            "open": ready,
            "click": CapabilityDescriptor(
                status=CapabilityStatus.READY,
                target_modes=("coordinate", "view_id"),
                fields=("x", "y", "match"),
                notes="view_id 由 Runtime 解析为坐标；禁止 ActionIntent 直传 resource_id",
            ),
            "scroll": ready,
            "back": ready,
            "close": ready,
            "input": CapabilityDescriptor(
                status=CapabilityStatus.READY,
                fields=("text",),
                notes="ASCII 走 input text；CJK 走 clipboard+PASTE",
            ),
            "reset_app": ready,
            "warm_restore": ready,
            "deep_link": ready,
            "set_clipboard": ready,
            "open_external": CapabilityDescriptor(
                status=CapabilityStatus.READY,
                fields=("action", "data_uri", "authorized"),
                notes="白名单 Intent；必须 authorized=true",
            ),
            "inject_account": ready,
        },
        observations={
            "screenshot": ready,
            "identity.package": ready,
            "identity.activity": ready,
            "identity.screen_id": ready,
            "orientation": ready,
            # 可读文本来自 Accessibility Hierarchy，不是 OCR
            "visible_text": ready,
            # UI Hierarchy / Snapshot 为有界制品，不等于 DOM；OCR 仍未接入。
            # ready＝契约可调用，不保证每帧 Capture 都持久化该制品。
            "ui_tree": CapabilityDescriptor(
                status=CapabilityStatus.READY,
                notes=(
                    "Accessibility uiautomator dump 可用；常作运行时门禁／解析，"
                    "不等于与截图同帧，也不保证每帧落库"
                ),
            ),
            "view_hierarchy": CapabilityDescriptor(
                status=CapabilityStatus.READY,
                notes=(
                    "AdbDeviceOps.dump_view_hierarchy 原语存在；"
                    "Official／Xingyu 主路径默认不持久化，不得假设每帧有制品"
                ),
            ),
            "ui_snapshot": ready,
            "media_state": media_state,
            "device_log": ready,
            "network_trace": (
                ready
                if _android_network_proxy_ready()
                else CapabilityDescriptor(status=CapabilityStatus.PARTIAL)
            ),
            # har 与 network_trace 同源：代理启用才有真实状态码；否则为空 entries 负证据
            "har": (
                ready
                if _android_network_proxy_ready()
                else CapabilityDescriptor(
                    status=CapabilityStatus.PARTIAL,
                    notes="由 network_trace 投影；未启用会话代理时仅空 entries",
                )
            ),
            "temporal_trace": CapabilityDescriptor(
                status=CapabilityStatus.READY,
                notes="请求 temporal_trace 时真实连拍；默认 3@1000ms，开屏类抬到 12",
            ),
            "fault_artifact": ready,
            "safe_area": ready,
            "ocr_text": unsupported,
        },
        artifacts={
            "screenshot": ready,
            "action_trace": ready,
            "ui_tree": CapabilityDescriptor(
                status=CapabilityStatus.READY,
                notes="可采集；主路径以 screenshot 持久化为主，勿假设每帧都有 ui_tree 制品",
            ),
            "view_hierarchy": CapabilityDescriptor(
                status=CapabilityStatus.READY,
                notes="原语可用；Official 主路径默认不落库",
            ),
            "ui_snapshot": ready,
            "visible_text": ready,
            "media_state": media_state,
            "device_log": ready,
            "network_trace": (
                ready
                if _android_network_proxy_ready()
                else CapabilityDescriptor(status=CapabilityStatus.PARTIAL)
            ),
            "har": (
                ready
                if _android_network_proxy_ready()
                else CapabilityDescriptor(
                    status=CapabilityStatus.PARTIAL,
                    notes="由 network_trace 投影为 format=network_events；无代理不伪造状态码",
                )
            ),
            "temporal_trace": CapabilityDescriptor(
                status=CapabilityStatus.READY,
                notes=(
                    "请求 temporal_trace 时 Official／Fake 真实连拍；"
                    "默认 3@1000ms，startup/splash/countdown 抬到 12；禁止同图复制冒充"
                ),
            ),
            "fault_artifact": ready,
            "dom": unsupported,
            "ocr_text": unsupported,
        },
    )


def _android_network_proxy_ready() -> bool:
    """代理启用时 network_trace／har 升为 READY；否则保持 partial（dumpsys／空投影）。"""
    try:
        from services.api.app.core.config import get_settings

        return bool(get_settings().workflow.app_network_proxy_enabled)
    except Exception:  # noqa: BLE001
        return False


def _validate_text_tuple(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise InteractionValidationError(f"{field_name} 必须为字符串集合")
    frozen = tuple(values)
    if any(not isinstance(item, str) or not item.strip() for item in frozen):
        raise InteractionValidationError(f"{field_name} 必须为非空字符串集合")
    normalized = tuple(item.strip() for item in frozen)
    if len(normalized) != len(set(normalized)):
        raise InteractionValidationError(f"{field_name} 不能重复")
    return normalized


def _read_sequence(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise InteractionValidationError(f"{field_name} 必须为字符串集合")
    return tuple(value)


def _read_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InteractionValidationError(f"{field_name} 必须为对象")
    return value


def _read_descriptors(value: Any, field_name: str) -> dict[str, CapabilityDescriptor]:
    mapping = _read_mapping(value, field_name)
    result: dict[str, CapabilityDescriptor] = {}
    for name, descriptor in mapping.items():
        if not isinstance(name, str) or not name.strip():
            raise InteractionValidationError(f"{field_name} 能力名称不能为空")
        if not isinstance(descriptor, Mapping):
            raise InteractionValidationError(f"{field_name}.{name} 必须为对象")
        result[name.strip()] = CapabilityDescriptor.from_dict(descriptor)
    return result


def _freeze_descriptors(
    values: Mapping[str, CapabilityDescriptor],
    field_name: str,
) -> Mapping[str, CapabilityDescriptor]:
    if not isinstance(values, Mapping) or not values:
        raise InteractionValidationError(f"{field_name} 不能为空")
    frozen: dict[str, CapabilityDescriptor] = {}
    for name, descriptor in values.items():
        if not isinstance(name, str) or not name.strip():
            raise InteractionValidationError(f"{field_name} 能力名称不能为空")
        if not isinstance(descriptor, CapabilityDescriptor):
            raise InteractionValidationError(f"{field_name}.{name} 类型不正确")
        normalized = name.strip()
        if normalized in frozen:
            raise InteractionValidationError(f"{field_name} 能力名称不能重复")
        frozen[normalized] = descriptor
    return MappingProxyType(dict(sorted(frozen.items())))


def _available_names(values: Mapping[str, CapabilityDescriptor]) -> tuple[str, ...]:
    return tuple(
        name
        for name, descriptor in values.items()
        if descriptor.status in {CapabilityStatus.READY, CapabilityStatus.PARTIAL}
    )


def _descriptor_payload(
    values: Mapping[str, CapabilityDescriptor],
) -> dict[str, dict[str, Any]]:
    return {name: descriptor.to_dict() for name, descriptor in values.items()}
