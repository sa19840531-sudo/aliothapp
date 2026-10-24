"""App 产品目标画像与包名注册表。

画像只声明产品级覆盖合同，不包含坐标、选择器、Playbook 或任何设备操作脚本。
包名是输入边界的一部分；同一包名被两个不同画像声明时必须失败关闭，不能依赖
导入顺序覆盖前一个注册。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.api.app.domain.exploration.generic5_coverage_scope import (
    GENERIC5_EXCLUDED_FROM_DENOMINATOR,
    GENERIC5_PACKAGE_NAMES,
    GENERIC5_REQUIRED_PAGE_TYPES,
)
from services.api.app.domain.exploration.hjll_coverage_scope import (
    HJLL_PACKAGE_NAME,
    HJLL_REQUIRED_PAGE_TYPES,
)
from services.api.app.domain.exploration.hlapp_coverage_scope import (
    HLAPP_PACKAGE_NAMES,
    HLAPP_REQUIRED_PAGE_TYPES,
)
from services.api.app.domain.exploration.pc51_coverage_scope import (
    PC51_PACKAGE_NAME,
    PC51_REQUIRED_PAGE_TYPES,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.protocol import (
    ProtocolContractError,
    read_known_fields,
    require_sequence,
    require_text,
    stable_protocol_hash,
)

APP_TARGET_PROFILE_PROTOCOL_VERSION = "app-target-profile.v1"
APP_TARGET_APPLICABILITY_DYNAMIC = "dynamic_discovery"
APP_TARGET_APPLICABILITY_EXPLICIT = "explicit_targets"
APP_TARGET_APPLICABILITY_PROFILE = "profile_declared"
_SUPPORTED_APPLICABILITY_MODES = frozenset(
    {
        APP_TARGET_APPLICABILITY_DYNAMIC,
        APP_TARGET_APPLICABILITY_EXPLICIT,
        APP_TARGET_APPLICABILITY_PROFILE,
    }
)


class AppTargetProfileError(ValueError):
    """产品目标画像不满足可持久化合同。"""


class AppTargetProfileConflictError(AppTargetProfileError):
    """多个画像声明了冲突的 profile 或包名。"""


@dataclass(frozen=True, kw_only=True)
class AppTargetProfile:
    """一个可复用的产品级页面适用性画像。

    ``required_page_types`` 和 ``optional_page_types`` 只能引用业务 Catalog；
    ``lifecycle_page_types`` 只能引用 Catalog 生命周期页型。画像不携带任何
    点击坐标或页面脚本，因而不会把某个产品的操作路径泄漏到通用探索器。
    """

    profile_id: str
    package_names: tuple[str, ...]
    required_page_types: tuple[str, ...]
    optional_page_types: tuple[str, ...] = ()
    lifecycle_page_types: tuple[str, ...] = ()
    required_milestones: tuple[str, ...] = ()
    minimum_business_pages: int | None = None
    device_capability_template: str = "android_visual_readonly_v1"
    completion_mode: str = APP_TARGET_APPLICABILITY_EXPLICIT
    applicability_mode: str = APP_TARGET_APPLICABILITY_PROFILE
    protocol_version: str = APP_TARGET_PROFILE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        profile_id = _text(self.profile_id, "profile_id")
        packages = _unique_texts(self.package_names, "package_names", casefold=True)
        required = _unique_texts(self.required_page_types, "required_page_types", casefold=True)
        optional = _unique_texts(self.optional_page_types, "optional_page_types", casefold=True)
        lifecycle = _unique_texts(
            self.lifecycle_page_types,
            "lifecycle_page_types",
            casefold=True,
        )
        milestones = _unique_texts(self.required_milestones, "required_milestones")
        if not packages:
            raise AppTargetProfileError("package_names 不能为空")
        if not required:
            raise AppTargetProfileError("required_page_types 不能为空")
        if set(required) & set(optional):
            raise AppTargetProfileError("required_page_types 与 optional_page_types 不能交叉")
        registry = get_default_page_type_registry()
        business = set(registry.coverage_required_codes)
        lifecycle_codes = set(registry.lifecycle_codes)
        invalid_required = sorted(set(required) - business)
        invalid_optional = sorted(set(optional) - business)
        invalid_lifecycle = sorted(set(lifecycle) - lifecycle_codes)
        if invalid_required:
            raise AppTargetProfileError(
                "required_page_types 不属于业务页面类型目录：" + ", ".join(invalid_required)
            )
        if invalid_optional:
            raise AppTargetProfileError(
                "optional_page_types 不属于业务页面类型目录：" + ", ".join(invalid_optional)
            )
        if invalid_lifecycle:
            raise AppTargetProfileError(
                "lifecycle_page_types 不属于生命周期页面类型目录："
                + ", ".join(invalid_lifecycle)
            )
        if self.minimum_business_pages is None:
            minimum = len(required)
        elif (
            isinstance(self.minimum_business_pages, bool)
            or not isinstance(self.minimum_business_pages, int)
            or not 1 <= self.minimum_business_pages <= 100
        ):
            raise AppTargetProfileError("minimum_business_pages 必须在 1 到 100 之间")
        else:
            minimum = self.minimum_business_pages
        if (
            not isinstance(self.device_capability_template, str)
            or not self.device_capability_template
        ):
            raise AppTargetProfileError("device_capability_template 不能为空")
        if self.completion_mode not in {
            APP_TARGET_APPLICABILITY_DYNAMIC,
            APP_TARGET_APPLICABILITY_EXPLICIT,
        }:
            raise AppTargetProfileError("completion_mode 不受支持")
        if self.applicability_mode not in _SUPPORTED_APPLICABILITY_MODES:
            raise AppTargetProfileError("applicability_mode 不受支持")
        if self.protocol_version != APP_TARGET_PROFILE_PROTOCOL_VERSION:
            raise AppTargetProfileError("AppTargetProfile protocol_version 不受支持")
        if not milestones:
            milestones = tuple(f"surface:{page_type}" for page_type in required)
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "package_names", packages)
        object.__setattr__(self, "required_page_types", required)
        object.__setattr__(self, "optional_page_types", optional)
        object.__setattr__(self, "lifecycle_page_types", lifecycle)
        object.__setattr__(self, "required_milestones", milestones)
        object.__setattr__(self, "minimum_business_pages", minimum)

    @property
    def profile_hash(self) -> str:
        """返回画像内容哈希，供注册冲突和目标快照审计使用。"""

        return "sha256:" + stable_protocol_hash(self.to_dict(include_hash=False))

    @property
    def applicability(self) -> dict[str, object]:
        """返回页面类型分组的只读 JSON 投影。"""

        return {
            "mode": self.applicability_mode,
            "required_page_types": list(self.required_page_types),
            "optional_page_types": list(self.optional_page_types),
            "lifecycle_page_types": list(self.lifecycle_page_types),
        }

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "profile_id": self.profile_id,
            "package_names": list(self.package_names),
            "required_page_types": list(self.required_page_types),
            "optional_page_types": list(self.optional_page_types),
            "lifecycle_page_types": list(self.lifecycle_page_types),
            "required_milestones": list(self.required_milestones),
            "minimum_business_pages": self.minimum_business_pages,
            "device_capability_template": self.device_capability_template,
            "completion_mode": self.completion_mode,
            "applicability_mode": self.applicability_mode,
            "applicability": self.applicability,
        }
        if include_hash:
            result["profile_hash"] = self.profile_hash
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AppTargetProfile:
        values = _read(
            payload,
            known={
                "protocol_version",
                "profile_id",
                "package_names",
                "required_page_types",
                "optional_page_types",
                "lifecycle_page_types",
                "required_milestones",
                "minimum_business_pages",
                "device_capability_template",
                "completion_mode",
                "applicability_mode",
                "applicability",
                "profile_hash",
            },
            required={
                "protocol_version",
                "profile_id",
                "package_names",
                "required_page_types",
                "optional_page_types",
                "lifecycle_page_types",
                "required_milestones",
                "minimum_business_pages",
                "device_capability_template",
                "completion_mode",
                "applicability_mode",
                "profile_hash",
            },
            field_name="AppTargetProfile",
        )
        applicability = values.get("applicability")
        if applicability is not None:
            if not isinstance(applicability, Mapping):
                raise AppTargetProfileError("applicability 必须为对象")
            mode = applicability.get("mode")
            if mode is not None and mode != values["applicability_mode"]:
                raise AppTargetProfileError("applicability.mode 与 applicability_mode 不一致")
            _check_profile_group_alias(
                values,
                applicability,
                "required_page_types",
            )
            _check_profile_group_alias(
                values,
                applicability,
                "optional_page_types",
            )
            _check_profile_group_alias(
                values,
                applicability,
                "lifecycle_page_types",
            )
        try:
            profile = cls(
                protocol_version=str(values["protocol_version"]),
                profile_id=str(values["profile_id"]),
                package_names=_sequence(values["package_names"], "package_names"),
                required_page_types=_sequence(
                    values["required_page_types"], "required_page_types"
                ),
                optional_page_types=_sequence(
                    values["optional_page_types"], "optional_page_types"
                ),
                lifecycle_page_types=_sequence(
                    values["lifecycle_page_types"], "lifecycle_page_types"
                ),
                required_milestones=_sequence(
                    values["required_milestones"], "required_milestones"
                ),
                minimum_business_pages=values["minimum_business_pages"],
                device_capability_template=str(values["device_capability_template"]),
                completion_mode=str(values["completion_mode"]),
                applicability_mode=str(values["applicability_mode"]),
            )
        except (ProtocolContractError, TypeError, ValueError) as exc:
            if isinstance(exc, AppTargetProfileError):
                raise
            raise AppTargetProfileError(str(exc)) from exc
        if values["profile_hash"] != profile.profile_hash:
            raise AppTargetProfileError("AppTargetProfile profile_hash 与内容不一致")
        return profile


class AppTargetProfileRegistry:
    """产品目标画像注册表，包名或 profile 冲突一律失败关闭。"""

    def __init__(self, profiles: Iterable[AppTargetProfile] = ()) -> None:
        self._profiles: dict[str, AppTargetProfile] = {}
        self._package_to_profile: dict[str, str] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: AppTargetProfile) -> None:
        if not isinstance(profile, AppTargetProfile):
            raise TypeError("profile 必须为 AppTargetProfile")
        existing = self._profiles.get(profile.profile_id)
        if existing is not None and existing != profile:
            raise AppTargetProfileConflictError(
                f"profile_id {profile.profile_id!r} 已注册且内容冲突"
            )
        for package_name in profile.package_names:
            key = package_name.casefold()
            existing_profile_id = self._package_to_profile.get(key)
            if existing_profile_id is not None and existing_profile_id != profile.profile_id:
                raise AppTargetProfileConflictError(
                    f"包名 {package_name!r} 同时属于 {existing_profile_id!r} 与 "
                    f"{profile.profile_id!r}"
                )
        self._profiles[profile.profile_id] = profile
        for package_name in profile.package_names:
            self._package_to_profile[package_name.casefold()] = profile.profile_id

    def resolve(self, package_name: str | None) -> AppTargetProfile | None:
        """按包名查询画像；提交入口不得把它当作默认产品适配器。"""

        if package_name is None:
            return None
        if not isinstance(package_name, str) or not package_name.strip():
            raise AppTargetProfileError("package_name 必须为非空字符串或空")
        profile_id = self._package_to_profile.get(package_name.strip().casefold())
        return self._profiles.get(profile_id) if profile_id is not None else None

    def get(self, profile_id: str) -> AppTargetProfile | None:
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise AppTargetProfileError("profile_id 必须为非空字符串")
        return self._profiles.get(profile_id.strip())

    @property
    def profiles(self) -> tuple[AppTargetProfile, ...]:
        return tuple(self._profiles.values())


def _check_profile_group_alias(
    values: Mapping[str, Any],
    applicability: Mapping[str, Any],
    field_name: str,
) -> None:
    alias = applicability.get(field_name)
    if alias is not None and _sequence(alias, f"applicability.{field_name}") != _sequence(
        values[field_name], field_name
    ):
        raise AppTargetProfileError(f"applicability.{field_name} 与顶层字段不一致")


def _read(
    payload: Mapping[str, Any],
    *,
    known: set[str],
    required: set[str],
    field_name: str,
) -> dict[str, Any]:
    try:
        return read_known_fields(
            payload,
            known_fields=frozenset(known),
            required_fields=frozenset(required),
            field_name=field_name,
        )
    except ProtocolContractError as exc:
        raise AppTargetProfileError(str(exc)) from exc


def _sequence(value: Any, field_name: str) -> tuple[Any, ...]:
    try:
        return require_sequence(value, field_name)
    except ProtocolContractError as exc:
        raise AppTargetProfileError(str(exc)) from exc


def _text(value: Any, field_name: str) -> str:
    try:
        return require_text(value, field_name)
    except ProtocolContractError as exc:
        raise AppTargetProfileError(str(exc)) from exc


def _unique_texts(
    values: Sequence[Any],
    field_name: str,
    *,
    casefold: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise AppTargetProfileError(f"{field_name} 必须为字符串数组")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _text(value, field_name)
        key = normalized.casefold() if casefold else normalized
        if key in seen:
            raise AppTargetProfileError(f"{field_name} 不能重复")
        seen.add(key)
        result.append(key if casefold else normalized)
    return tuple(result)


def _default_profile_registry() -> AppTargetProfileRegistry:
    registry = get_default_page_type_registry()
    lifecycle = tuple(registry.lifecycle_codes)
    profiles = (
        AppTargetProfile(
            profile_id="hjll",
            package_names=(HJLL_PACKAGE_NAME,),
            required_page_types=HJLL_REQUIRED_PAGE_TYPES,
            optional_page_types=(
                "profile",
                "settings",
                "payment",
                "post_publish",
                "activity",
            ),
            lifecycle_page_types=lifecycle,
        ),
        AppTargetProfile(
            profile_id="pc51",
            package_names=(PC51_PACKAGE_NAME,),
            required_page_types=PC51_REQUIRED_PAGE_TYPES,
            optional_page_types=(
                "profile",
                "settings",
                "payment",
                "post_publish",
                "post_list",
                "activity",
            ),
            lifecycle_page_types=lifecycle,
        ),
        AppTargetProfile(
            profile_id="hlapp",
            package_names=HLAPP_PACKAGE_NAMES,
            required_page_types=HLAPP_REQUIRED_PAGE_TYPES,
            optional_page_types=("profile", "settings", "payment", "post_publish"),
            lifecycle_page_types=lifecycle,
        ),
        AppTargetProfile(
            profile_id="generic5",
            package_names=tuple(sorted(GENERIC5_PACKAGE_NAMES)),
            required_page_types=GENERIC5_REQUIRED_PAGE_TYPES,
            optional_page_types=GENERIC5_EXCLUDED_FROM_DENOMINATOR,
            lifecycle_page_types=lifecycle,
        ),
    )
    return AppTargetProfileRegistry(profiles)


_DEFAULT_APP_TARGET_PROFILE_REGISTRY = _default_profile_registry()


def get_default_app_target_profile_registry() -> AppTargetProfileRegistry:
    """返回默认产品画像注册表。调用方不得在运行期间修改它。"""

    return _DEFAULT_APP_TARGET_PROFILE_REGISTRY


def register_app_target_profile(profile: AppTargetProfile) -> None:
    """为启动期扩展注册表；冲突时直接抛错，禁止静默覆盖。"""

    _DEFAULT_APP_TARGET_PROFILE_REGISTRY.register(profile)
