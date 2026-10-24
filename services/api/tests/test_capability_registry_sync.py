"""Capability Manifest 数据库同步测试。"""

from typing import Any

import pytest

from services.api.app.agents.capability_manifest import (
    CapabilityKind,
    CapabilityManifestEntry,
)
from services.api.app.application.capability_registry import (
    CapabilityPermission,
    CapabilityRegistrySyncService,
)
from services.api.app.infrastructure.db.capability_registry_repository import (
    PostgresCapabilityRegistryRepository,
)


class FakeCapabilityRegistryRepository:
    def __init__(self, disabled_count: int = 0) -> None:
        self.disabled_count = disabled_count
        self.calls: list[tuple[Any, ...]] = []

    async def sync(self, capabilities: tuple[Any, ...]) -> int:
        self.calls.append(capabilities)
        return self.disabled_count


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        normalized_query = " ".join(query.split())
        self.execute_calls.append((normalized_query, args))
        if normalized_query.startswith("WITH manifest_entries"):
            return "UPDATE 2"
        return "OK"


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


def _manifest() -> tuple[CapabilityManifestEntry, ...]:
    return (
        CapabilityManifestEntry(
            name="safe_tool",
            kind=CapabilityKind.TOOL,
            description="只读工具",
            implementation_ref="tests:safe_tool",
            runtime_permission="safe",
            input_schema={"type": "object"},
        ),
        CapabilityManifestEntry(
            name="sensitive_tool",
            kind=CapabilityKind.TOOL,
            description="写入工具",
            implementation_ref="tests:sensitive_tool",
            runtime_permission="sensitive",
        ),
        CapabilityManifestEntry(
            name="payment-rule",
            kind=CapabilityKind.SKILL,
            description="支付规则",
            implementation_ref="skills/payment-rule/SKILL.md",
        ),
    )


async def test_sync_service_projects_permissions_and_returns_counts() -> None:
    repository = FakeCapabilityRegistryRepository(disabled_count=2)
    service = CapabilityRegistrySyncService(repository=repository)

    result = await service.sync(_manifest())

    capabilities = repository.calls[0]
    assert result.synced_count == 3
    assert result.disabled_count == 2
    assert capabilities[0].permission_level == CapabilityPermission.LOW
    assert capabilities[1].permission_level == CapabilityPermission.MEDIUM
    assert capabilities[2].permission_level == CapabilityPermission.HIGH_RISK
    assert capabilities[0].input_schema == {"type": "object"}
    assert capabilities[2].input_schema == {}
    assert all(item.status == "enabled" for item in capabilities)


async def test_sync_service_rejects_empty_manifest() -> None:
    service = CapabilityRegistrySyncService(repository=FakeCapabilityRegistryRepository())

    with pytest.raises(ValueError, match="Capability Manifest 不能为空"):
        await service.sync(())


async def test_sync_service_rejects_unknown_runtime_permission() -> None:
    service = CapabilityRegistrySyncService(repository=FakeCapabilityRegistryRepository())
    manifest = (
        CapabilityManifestEntry(
            name="unknown_tool",
            kind=CapabilityKind.TOOL,
            description="未知权限工具",
            implementation_ref="tests:unknown_tool",
            runtime_permission="unknown",
        ),
    )

    with pytest.raises(ValueError, match="不支持的运行时权限级别：unknown"):
        await service.sync(manifest)


async def test_postgres_repository_upserts_and_disables_stale_entries() -> None:
    pool = FakePool()
    repository = PostgresCapabilityRegistryRepository(pool=pool)
    service = CapabilityRegistrySyncService(repository=repository)

    result = await service.sync(_manifest())

    calls = pool.connection.execute_calls
    assert "pg_advisory_xact_lock" in calls[0][0]
    upsert_calls = [call for call in calls if call[0].startswith("INSERT INTO")]
    assert len(upsert_calls) == 3
    assert "ON CONFLICT (capability_name, version)" in upsert_calls[0][0]
    assert upsert_calls[0][1][0] == "safe_tool"
    assert upsert_calls[0][1][7] == "low"
    assert upsert_calls[1][1][7] == "medium"
    assert upsert_calls[2][1][7] == "high_risk"
    disable_query, disable_args = calls[-1]
    assert disable_query.startswith("WITH manifest_entries")
    assert "status IN ('draft', 'enabled')" in disable_query
    assert result.disabled_count == 2
    assert '"capability_name": "safe_tool"' in disable_args[0]
