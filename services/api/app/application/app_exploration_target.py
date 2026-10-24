"""App 任务提交时锁定的探索目标与设备能力模板。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.api.app.domain.exploration.app_target_profile import (
    APP_TARGET_APPLICABILITY_DYNAMIC,
    APP_TARGET_APPLICABILITY_EXPLICIT,
    APP_TARGET_APPLICABILITY_PROFILE,
    AppTargetProfile,
    AppTargetProfileError,
    AppTargetProfileRegistry,
    get_default_app_target_profile_registry,
)
from services.api.app.domain.interaction import SurfaceCapabilityManifest
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.protocol import stable_protocol_hash

APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V1 = "app-exploration-target.v1"
APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V2 = "app-exploration-target.v2"
APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V3 = "app-exploration-target.v3"
APP_EXPLORATION_TARGET_PROTOCOL_VERSION = APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V3
APP_EXPLORATION_TARGET_COMPLETION_MODE_DYNAMIC = "dynamic_discovery"
APP_EXPLORATION_TARGET_COMPLETION_MODE_EXPLICIT = "explicit_targets"
_SUPPORTED_COMPLETION_MODES = frozenset(
    {
        APP_EXPLORATION_TARGET_COMPLETION_MODE_DYNAMIC,
        APP_EXPLORATION_TARGET_COMPLETION_MODE_EXPLICIT,
    }
)
_SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {
        APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V1,
        APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V2,
        APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V3,
    }
)
_SUPPORTED_APPLICABILITY_MODES = frozenset(
    {
        APP_TARGET_APPLICABILITY_DYNAMIC,
        APP_TARGET_APPLICABILITY_EXPLICIT,
        APP_TARGET_APPLICABILITY_PROFILE,
    }
)
DEFAULT_APP_DEVICE_CAPABILITY_TEMPLATE = "android_visual_readonly_v1"

_CAPABILITY_TEMPLATES: dict[str, tuple[str, ...]] = {
    "android_visual_readonly_v1": (
        "session",
        "install",
        "open",
        "click",
        "scroll",
        "back",
        "close",
        "screenshot",
        "ui_tree",
    ),
    "android_visual_evidence_v1": (
        "session",
        "install",
        "open",
        "click",
        "scroll",
        "back",
        "close",
        "screenshot",
        "ui_tree",
        "visible_text",
        "ui_snapshot",
        "device_log",
    ),
}


class AppExplorationTargetError(ValueError):
    """提交目标快照无效、哈希漂移或设备能力不足。"""


@dataclass(frozen=True, kw_only=True)
class AppExplorationTargetSnapshot:
    """任务创建后不可扩大的 App 产品目标快照。"""

    required_page_types: tuple[str, ...]
    required_milestones: tuple[str, ...]
    minimum_business_pages: int
    device_capability_template: str
    requires_real_visual: bool
    target_hash: str
    completion_mode: str = APP_EXPLORATION_TARGET_COMPLETION_MODE_EXPLICIT
    protocol_version: str = APP_EXPLORATION_TARGET_PROTOCOL_VERSION
    optional_page_types: tuple[str, ...] = ()
    lifecycle_page_types: tuple[str, ...] = ()
    allowed_packages: tuple[str, ...] = ()
    profile_id: str | None = None
    applicability_mode: str = APP_TARGET_APPLICABILITY_DYNAMIC

    def __post_init__(self) -> None:
        page_types = _unique_texts(self.required_page_types, field_name="required_page_types")
        milestones = _unique_texts(self.required_milestones, field_name="required_milestones")
        optional_page_types = _unique_texts(
            self.optional_page_types,
            field_name="optional_page_types",
        )
        lifecycle_page_types = _unique_texts(
            self.lifecycle_page_types,
            field_name="lifecycle_page_types",
        )
        allowed_packages = _unique_texts(
            self.allowed_packages,
            field_name="allowed_packages",
            casefold=True,
        )
        if not page_types or not milestones:
            raise AppExplorationTargetError("App required page types/milestones 不能为空")
        if (
            isinstance(self.minimum_business_pages, bool)
            or not isinstance(self.minimum_business_pages, int)
            or not 1 <= self.minimum_business_pages <= 100
        ):
            raise AppExplorationTargetError("minimum_business_pages 必须在 1 到 100 之间")
        if self.device_capability_template not in _CAPABILITY_TEMPLATES:
            raise AppExplorationTargetError("device_capability_template 不受支持")
        if not isinstance(self.requires_real_visual, bool):
            raise AppExplorationTargetError("requires_real_visual 必须为布尔值")
        if self.protocol_version not in _SUPPORTED_PROTOCOL_VERSIONS:
            raise AppExplorationTargetError("App exploration target protocol_version 不受支持")
        completion_mode = self.completion_mode
        if self.protocol_version == APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V1:
            # v1 没有 completion_mode 字段；历史快照始终按显式目标解释。
            completion_mode = APP_EXPLORATION_TARGET_COMPLETION_MODE_EXPLICIT
        elif completion_mode not in _SUPPORTED_COMPLETION_MODES:
            raise AppExplorationTargetError("App exploration target completion_mode 不受支持")
        if self.protocol_version != APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V3:
            optional_page_types = ()
            lifecycle_page_types = ()
            allowed_packages = ()
            profile_id = None
            applicability_mode = (
                APP_TARGET_APPLICABILITY_DYNAMIC
                if completion_mode == APP_EXPLORATION_TARGET_COMPLETION_MODE_DYNAMIC
                else APP_TARGET_APPLICABILITY_EXPLICIT
            )
        else:
            profile_id = _optional_text(self.profile_id, field_name="profile_id")
            applicability_mode = self.applicability_mode
            if applicability_mode not in _SUPPORTED_APPLICABILITY_MODES:
                raise AppExplorationTargetError(
                    "App exploration target applicability_mode 不受支持"
                )
            if (
                applicability_mode == APP_TARGET_APPLICABILITY_DYNAMIC
                and completion_mode != APP_EXPLORATION_TARGET_COMPLETION_MODE_DYNAMIC
            ):
                raise AppExplorationTargetError(
                    "dynamic_discovery 目标必须使用 dynamic_discovery completion_mode"
                )
            if (
                applicability_mode == APP_TARGET_APPLICABILITY_EXPLICIT
                and completion_mode != APP_EXPLORATION_TARGET_COMPLETION_MODE_EXPLICIT
            ):
                raise AppExplorationTargetError(
                    "explicit_targets 目标必须使用 explicit_targets completion_mode"
                )
            if set(page_types) & set(optional_page_types):
                raise AppExplorationTargetError(
                    "required_page_types 与 optional_page_types 不能交叉"
                )
            registry = get_default_page_type_registry()
            invalid_required = sorted(set(page_types) - set(registry.coverage_required_codes))
            business_codes = set(registry.coverage_required_codes)
            lifecycle_codes = set(registry.lifecycle_codes)
            if invalid_required:
                raise AppExplorationTargetError(
                    "required_page_types 不属于业务页面类型目录："
                    + ", ".join(invalid_required)
                )
            invalid_optional = sorted(set(optional_page_types) - business_codes)
            invalid_lifecycle = sorted(set(lifecycle_page_types) - lifecycle_codes)
            if invalid_optional:
                raise AppExplorationTargetError(
                    "optional_page_types 不属于业务页面类型目录："
                    + ", ".join(invalid_optional)
                )
            if invalid_lifecycle:
                raise AppExplorationTargetError(
                    "lifecycle_page_types 不属于生命周期页面类型目录："
                    + ", ".join(invalid_lifecycle)
                )
            if profile_id is None and applicability_mode == APP_TARGET_APPLICABILITY_PROFILE:
                raise AppExplorationTargetError(
                    "profile_declared 目标必须提供 profile_id"
                )
            if profile_id is not None and applicability_mode != APP_TARGET_APPLICABILITY_PROFILE:
                raise AppExplorationTargetError(
                    "带 profile_id 的目标必须使用 profile_declared applicability_mode"
                )
        object.__setattr__(self, "required_page_types", page_types)
        object.__setattr__(self, "required_milestones", milestones)
        object.__setattr__(self, "completion_mode", completion_mode)
        object.__setattr__(self, "optional_page_types", optional_page_types)
        object.__setattr__(self, "lifecycle_page_types", lifecycle_page_types)
        object.__setattr__(self, "allowed_packages", allowed_packages)
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "applicability_mode", applicability_mode)
        if self.target_hash != self.recompute_target_hash():
            raise AppExplorationTargetError("App exploration target hash 与内容不一致")

    def recompute_target_hash(self) -> str:
        return "sha256:" + stable_protocol_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "required_page_types": list(self.required_page_types),
            "required_milestones": list(self.required_milestones),
            "minimum_business_pages": self.minimum_business_pages,
            "device_capability_template": self.device_capability_template,
            "requires_real_visual": self.requires_real_visual,
        }
        if self.protocol_version != APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V1:
            result["completion_mode"] = self.completion_mode
        if self.protocol_version == APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V3:
            result.update(
                {
                    "optional_page_types": list(self.optional_page_types),
                    "lifecycle_page_types": list(self.lifecycle_page_types),
                    "allowed_packages": list(self.allowed_packages),
                    "profile_id": self.profile_id,
                    "applicability_mode": self.applicability_mode,
                    "applicability": self.applicability,
                }
            )
        if include_hash:
            result["target_hash"] = self.target_hash
        return result

    @property
    def applicability(self) -> dict[str, object]:
        """返回目标页面分组，作为报告与兼容消费者的稳定投影。"""

        return {
            "mode": self.applicability_mode,
            "required_page_types": list(self.required_page_types),
            "optional_page_types": list(self.optional_page_types),
            "lifecycle_page_types": list(self.lifecycle_page_types),
        }

    def require_device_capabilities(self, manifest: SurfaceCapabilityManifest) -> None:
        if not isinstance(manifest, SurfaceCapabilityManifest):
            raise TypeError("manifest 必须为 SurfaceCapabilityManifest")
        try:
            manifest.require_ready(_CAPABILITY_TEMPLATES[self.device_capability_template])
        except ValueError as exc:
            raise AppExplorationTargetError("App 设备能力不满足锁定模板") from exc

    def require_installed_package(self, package_name: str) -> None:
        """校验 APP_READY 实际包名仍属于提交时锁定的产品范围。"""

        installed_package = _optional_text(package_name, field_name="package_name")
        if installed_package is None:
            raise AppExplorationTargetError("APP_READY 未返回实际安装包名")
        if self.allowed_packages and installed_package.casefold() not in {
            item.casefold() for item in self.allowed_packages
        }:
            raise AppExplorationTargetError("实际安装包不属于锁定目标包集合")

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> AppExplorationTargetSnapshot:
        if not isinstance(payload, Mapping):
            raise AppExplorationTargetError("App exploration target 必须为对象")
        protocol_version = str(payload.get("protocol_version") or "")
        completion_mode = (
            APP_EXPLORATION_TARGET_COMPLETION_MODE_EXPLICIT
            if protocol_version == APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V1
            else str(payload.get("completion_mode") or "")
        )
        optional_page_types: tuple[str, ...] = ()
        lifecycle_page_types: tuple[str, ...] = ()
        allowed_packages: tuple[str, ...] = ()
        profile_id: str | None = None
        applicability_mode = (
            APP_TARGET_APPLICABILITY_DYNAMIC
            if completion_mode == APP_EXPLORATION_TARGET_COMPLETION_MODE_DYNAMIC
            else APP_TARGET_APPLICABILITY_EXPLICIT
        )
        if protocol_version == APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V3:
            optional_page_types = _sequence(
                payload.get("optional_page_types", ()),
            )
            lifecycle_page_types = _sequence(
                payload.get("lifecycle_page_types", ()),
            )
            allowed_packages = _sequence(payload.get("allowed_packages", ()))
            profile_id = _optional_text(payload.get("profile_id"), field_name="profile_id")
            applicability_mode = str(
                payload.get("applicability_mode") or applicability_mode
            )
            applicability = payload.get("applicability")
            if applicability is not None:
                if not isinstance(applicability, Mapping):
                    raise AppExplorationTargetError("applicability 必须为对象")
                applicability_mode = _merge_applicability_mode(
                    applicability_mode,
                    applicability,
                )
                optional_page_types = _merge_applicability_sequence(
                    optional_page_types,
                    applicability,
                    "optional_page_types",
                )
                lifecycle_page_types = _merge_applicability_sequence(
                    lifecycle_page_types,
                    applicability,
                    "lifecycle_page_types",
                )
                required_from_applicability = applicability.get("required_page_types")
                if required_from_applicability is not None:
                    required_page_types = _sequence(payload.get("required_page_types"))
                    if tuple(required_from_applicability) != required_page_types:
                        raise AppExplorationTargetError(
                            "applicability.required_page_types 与顶层字段不一致"
                        )
        return cls(
            protocol_version=protocol_version,
            required_page_types=_sequence(payload.get("required_page_types")),
            required_milestones=_sequence(payload.get("required_milestones")),
            minimum_business_pages=_integer(payload.get("minimum_business_pages")),
            device_capability_template=str(payload.get("device_capability_template") or ""),
            requires_real_visual=payload.get("requires_real_visual") is True,
            target_hash=str(payload.get("target_hash") or ""),
            completion_mode=completion_mode,
            optional_page_types=optional_page_types,
            lifecycle_page_types=lifecycle_page_types,
            allowed_packages=allowed_packages,
            profile_id=profile_id,
            applicability_mode=applicability_mode,
        )


def build_app_exploration_target_snapshot(
    *,
    journey_steps: Sequence[str],
    package_name: str | None = None,
    required_page_types: Sequence[str] = (),
    optional_page_types: Sequence[str] = (),
    lifecycle_page_types: Sequence[str] = (),
    required_milestones: Sequence[str] = (),
    minimum_business_pages: int | None = None,
    device_capability_template: str = DEFAULT_APP_DEVICE_CAPABILITY_TEMPLATE,
    profile_id: str | None = None,
    applicability_mode: str | None = None,
    profile_registry: AppTargetProfileRegistry | None = None,
) -> AppExplorationTargetSnapshot:
    """在任务提交边界生成一次目标，运行期只能验证和读取。"""

    journeys = _unique_texts(journey_steps, field_name="journey_steps")
    normalized_package_name = _optional_text(package_name, field_name="package_name")
    normalized_profile_id = _optional_text(profile_id, field_name="profile_id")
    # 包名只用于锁定运行期的目标边界；只有调用方显式声明 profile_id 时，
    # 才允许读取产品画像合同。动态探索不能因命中已知包名而自动变成产品适配。
    profile: AppTargetProfile | None = None
    if normalized_profile_id is not None:
        registry = profile_registry or get_default_app_target_profile_registry()
        try:
            profile = registry.get(normalized_profile_id)
        except AppTargetProfileError as exc:
            raise AppExplorationTargetError(str(exc)) from exc
        if profile is None:
            raise AppExplorationTargetError(
                f"App 产品目标 profile_id 未注册：{normalized_profile_id}"
            )
    if profile is not None:
        normalized_profile_id = profile.profile_id
        if normalized_package_name is not None and normalized_package_name.casefold() not in {
            item.casefold() for item in profile.package_names
        }:
            raise AppExplorationTargetError("package_name 不属于注册的产品目标 profile")
        _reject_profile_override(
            required_page_types,
            profile.required_page_types,
            field_name="required_page_types",
        )
        _reject_profile_override(
            optional_page_types,
            profile.optional_page_types,
            field_name="optional_page_types",
        )
        _reject_profile_override(
            lifecycle_page_types,
            profile.lifecycle_page_types,
            field_name="lifecycle_page_types",
        )
        explicit_page_types = profile.required_page_types
        optional_page_types = profile.optional_page_types
        lifecycle_page_types = profile.lifecycle_page_types
        if minimum_business_pages is None:
            minimum_business_pages = profile.minimum_business_pages
        if not required_milestones:
            required_milestones = profile.required_milestones
        if device_capability_template == DEFAULT_APP_DEVICE_CAPABILITY_TEMPLATE:
            device_capability_template = profile.device_capability_template
        completion_mode = profile.completion_mode
        resolved_applicability_mode = profile.applicability_mode
        allowed_packages = profile.package_names
        if (
            applicability_mode is not None
            and applicability_mode != resolved_applicability_mode
        ):
            raise AppExplorationTargetError(
                "applicability_mode 与注册产品目标 profile 不一致，已失败关闭"
            )
    else:
        explicit_page_types = _unique_texts(
            required_page_types,
            field_name="required_page_types",
        )
        optional_page_types = _unique_texts(
            optional_page_types,
            field_name="optional_page_types",
        )
        lifecycle_page_types = _unique_texts(
            lifecycle_page_types,
            field_name="lifecycle_page_types",
        )
        completion_mode = (
            APP_EXPLORATION_TARGET_COMPLETION_MODE_EXPLICIT
            if explicit_page_types
            else APP_EXPLORATION_TARGET_COMPLETION_MODE_DYNAMIC
        )
        resolved_applicability_mode = (
            applicability_mode
            or (
                APP_TARGET_APPLICABILITY_EXPLICIT
                if explicit_page_types
                else APP_TARGET_APPLICABILITY_DYNAMIC
            )
        )
        allowed_packages = ((normalized_package_name,) if normalized_package_name else ())
    # 先按快照校验器的同一规则规范化包名，再计算哈希，避免大小写导致内容漂移。
    allowed_packages = _unique_texts(
        allowed_packages,
        field_name="allowed_packages",
        casefold=True,
    )
    page_registry = get_default_page_type_registry()
    invalid_page_types = sorted(
        set(explicit_page_types) - set(page_registry.coverage_required_codes)
    )
    if invalid_page_types:
        raise AppExplorationTargetError(
            "required_page_types 不属于业务页面类型目录：" + ", ".join(invalid_page_types)
        )
    invalid_optional_page_types = sorted(
        set(optional_page_types) - set(page_registry.coverage_required_codes)
    )
    if invalid_optional_page_types:
        raise AppExplorationTargetError(
            "optional_page_types 不属于业务页面类型目录："
            + ", ".join(invalid_optional_page_types)
        )
    invalid_lifecycle_page_types = sorted(
        set(lifecycle_page_types) - set(page_registry.lifecycle_codes)
    )
    if invalid_lifecycle_page_types:
        raise AppExplorationTargetError(
            "lifecycle_page_types 不属于生命周期页面类型目录："
            + ", ".join(invalid_lifecycle_page_types)
        )
    if set(explicit_page_types) & set(optional_page_types):
        raise AppExplorationTargetError("required_page_types 与 optional_page_types 不能交叉")
    if profile is None and resolved_applicability_mode == APP_TARGET_APPLICABILITY_PROFILE:
        raise AppExplorationTargetError("profile_declared 目标必须解析到已注册产品 profile")
    if (
        profile is None
        and resolved_applicability_mode == APP_TARGET_APPLICABILITY_EXPLICIT
        and not explicit_page_types
    ):
        raise AppExplorationTargetError("explicit_targets 目标必须提供 required_page_types")
    if (
        resolved_applicability_mode == APP_TARGET_APPLICABILITY_DYNAMIC
        and explicit_page_types
        and set(explicit_page_types) != set(page_registry.coverage_required_codes)
    ):
        raise AppExplorationTargetError(
            "dynamic_discovery 目标不能缩小 required_page_types，已失败关闭"
        )
    if resolved_applicability_mode == APP_TARGET_APPLICABILITY_DYNAMIC:
        completion_mode = APP_EXPLORATION_TARGET_COMPLETION_MODE_DYNAMIC
    page_types = explicit_page_types or page_registry.coverage_required_codes
    milestones = _unique_texts(required_milestones, field_name="required_milestones")
    if not milestones:
        milestones = (
            ("surface:stable_business_page",)
            if completion_mode == APP_EXPLORATION_TARGET_COMPLETION_MODE_DYNAMIC
            else (
                tuple(f"journey:{item}" for item in journeys)
                if journeys
                else tuple(f"surface:{item}" for item in page_types)
            )
        )
    minimum = (
        minimum_business_pages
        if minimum_business_pages is not None
        else (
            1
            if completion_mode == APP_EXPLORATION_TARGET_COMPLETION_MODE_DYNAMIC
            else max(2, len(journeys), len(page_types))
        )
    )
    values = {
        "protocol_version": APP_EXPLORATION_TARGET_PROTOCOL_VERSION,
        "required_page_types": page_types,
        "required_milestones": milestones,
        "minimum_business_pages": minimum,
        "device_capability_template": device_capability_template,
        "requires_real_visual": True,
        "completion_mode": completion_mode,
        "optional_page_types": optional_page_types,
        "lifecycle_page_types": lifecycle_page_types,
        "allowed_packages": allowed_packages,
        "profile_id": normalized_profile_id,
        "applicability_mode": resolved_applicability_mode,
    }
    hash_values = {
        **values,
        "applicability": _applicability_payload(
            mode=resolved_applicability_mode,
            required_page_types=page_types,
            optional_page_types=optional_page_types,
            lifecycle_page_types=lifecycle_page_types,
        ),
    }
    return AppExplorationTargetSnapshot(
        **values,
        target_hash="sha256:" + stable_protocol_hash(hash_values),
    )


def read_locked_app_exploration_target(
    task_config: Mapping[str, object],
) -> AppExplorationTargetSnapshot | None:
    """新任务严格读取哈希快照；旧任务显式返回 None 走兼容口径。"""

    payload = task_config.get("app_exploration_target")
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise AppExplorationTargetError("task_config.app_exploration_target 必须为对象")
    return AppExplorationTargetSnapshot.from_dict(payload)


def supported_device_capability_templates() -> tuple[str, ...]:
    return tuple(_CAPABILITY_TEMPLATES)


def _unique_texts(
    values: Sequence[str],
    *,
    field_name: str,
    casefold: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise AppExplorationTargetError(f"{field_name} 必须为字符串数组")
    result: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip() or item != item.strip():
            raise AppExplorationTargetError(f"{field_name} 必须为无首尾空白的非空字符串")
        normalized = (
            item.casefold()
            if casefold
            or field_name
            in {"required_page_types", "optional_page_types", "lifecycle_page_types"}
            else item
        )
        if len(normalized) > 256:
            raise AppExplorationTargetError(f"{field_name} 单项不能超过 256 字符")
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise AppExplorationTargetError("App exploration target 数组字段无效")
    return tuple(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AppExplorationTargetError("App exploration target 整数字段无效")
    return value


def _optional_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise AppExplorationTargetError(f"{field_name} 必须为无首尾空白的非空字符串或空")
    return value


def _applicability_payload(
    *,
    mode: str,
    required_page_types: Sequence[str],
    optional_page_types: Sequence[str],
    lifecycle_page_types: Sequence[str],
) -> dict[str, object]:
    return {
        "mode": mode,
        "required_page_types": list(required_page_types),
        "optional_page_types": list(optional_page_types),
        "lifecycle_page_types": list(lifecycle_page_types),
    }


def _merge_applicability_mode(
    current: str,
    applicability: Mapping[str, object],
) -> str:
    declared = applicability.get("mode")
    if declared is None:
        return current
    declared_mode = str(declared)
    if declared_mode != current:
        raise AppExplorationTargetError("applicability.mode 与 applicability_mode 不一致")
    return declared_mode


def _merge_applicability_sequence(
    current: tuple[str, ...],
    applicability: Mapping[str, object],
    field_name: str,
) -> tuple[str, ...]:
    declared = applicability.get(field_name)
    if declared is None:
        return current
    declared_values = _sequence(declared)
    if current and tuple(declared_values) != tuple(current):
        raise AppExplorationTargetError(f"applicability.{field_name} 与顶层字段不一致")
    return tuple(declared_values)


def _reject_profile_override(
    requested: Sequence[str],
    registered: Sequence[str],
    *,
    field_name: str,
) -> None:
    """注册画像的页面分组不可被任务请求静默改写。"""

    if not requested:
        return
    normalized = _unique_texts(
        requested,
        field_name=field_name,
        casefold=True,
    )
    registered_normalized = tuple(item.casefold() for item in registered)
    if normalized != registered_normalized:
        raise AppExplorationTargetError(
            f"{field_name} 与注册产品目标 profile 不一致，已失败关闭"
        )
