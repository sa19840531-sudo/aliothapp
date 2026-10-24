"""Capability Manifest 到数据库注册表的同步编排。"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from services.api.app.agents.capability_manifest import CapabilityManifestEntry


class CapabilityPermission(StrEnum):
    """数据库能力权限级别。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH_RISK = "high_risk"


DEFAULT_PERMISSION_OVERRIDES: Mapping[str, CapabilityPermission] = {
    "payment-rule": CapabilityPermission.HIGH_RISK,
}


@dataclass(frozen=True)
class CapabilityRegistryEntry:
    """写入数据库的能力注册表投影。"""

    capability_name: str
    capability_kind: str
    display_name: str
    description: str
    version: str
    status: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permission_level: CapabilityPermission
    timeout_ms: int
    retry_policy: dict[str, Any]
    owner_team: str
    implementation_ref: str


@dataclass(frozen=True)
class CapabilityRegistrySyncResult:
    """一次能力注册表同步结果。"""

    synced_count: int
    disabled_count: int


class CapabilityRegistryRepository(Protocol):
    """能力注册表持久化契约。"""

    async def sync(self, capabilities: tuple[CapabilityRegistryEntry, ...]) -> int: ...


class CapabilityRegistrySyncService:
    """将运行时Manifest转换为数据库投影并执行同步。"""

    def __init__(
        self,
        *,
        repository: CapabilityRegistryRepository,
        permission_overrides: Mapping[str, CapabilityPermission] | None = None,
    ) -> None:
        self._repository = repository
        self._permission_overrides = (
            DEFAULT_PERMISSION_OVERRIDES if permission_overrides is None else permission_overrides
        )

    async def sync(
        self,
        manifest: tuple[CapabilityManifestEntry, ...],
    ) -> CapabilityRegistrySyncResult:
        if not manifest:
            raise ValueError("Capability Manifest 不能为空")

        capabilities = tuple(self._project(item) for item in manifest)
        disabled_count = await self._repository.sync(capabilities)
        return CapabilityRegistrySyncResult(
            synced_count=len(capabilities),
            disabled_count=disabled_count,
        )

    def _project(self, item: CapabilityManifestEntry) -> CapabilityRegistryEntry:
        permission_level = self._permission_overrides.get(item.name)
        if permission_level is None:
            permission_level = _map_runtime_permission(item.runtime_permission)
        return CapabilityRegistryEntry(
            capability_name=item.name,
            capability_kind=item.kind.value,
            display_name=_display_name(item.name),
            description=item.description,
            version=item.version,
            status="enabled",
            input_schema=item.input_schema,
            output_schema={},
            permission_level=permission_level,
            timeout_ms=30000,
            retry_policy={},
            owner_team="ai-platform",
            implementation_ref=item.implementation_ref,
        )


def _map_runtime_permission(runtime_permission: str | None) -> CapabilityPermission:
    if runtime_permission in {None, "safe"}:
        return CapabilityPermission.LOW
    if runtime_permission == "sensitive":
        return CapabilityPermission.MEDIUM
    raise ValueError(f"不支持的运行时权限级别：{runtime_permission}")


def _display_name(capability_name: str) -> str:
    return capability_name.replace("-", " ").replace("_", " ").title()
