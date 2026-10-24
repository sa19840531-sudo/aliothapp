"""H5 交互模式仓储的幂等结果反馈测试。"""

from __future__ import annotations

from typing import Any

import pytest

from services.api.app.application.h5_adaptive_navigation import (
    H5AdaptiveNavigationItem,
    H5AdaptiveNavigationPattern,
)
from services.api.app.infrastructure.db.h5_interaction_pattern_repository import (
    PostgresH5InteractionPatternRepository,
)


class _AsyncContext:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *_: object) -> None:
        return None


class _ReplayConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def transaction(self) -> _AsyncContext:
        return _AsyncContext(self)

    async def execute(self, statement: str, *_: object) -> str:
        self.statements.append(" ".join(statement.split()))
        return "SELECT 1"

    async def fetchval(self, statement: str, *_: object) -> object:
        normalized = " ".join(statement.split())
        self.statements.append(normalized)
        if normalized.startswith("SELECT 1 FROM qc_h5_interaction_pattern"):
            return 1
        if normalized.startswith("INSERT INTO qc_h5_interaction_pattern_observation"):
            return None
        if normalized.startswith("SELECT status FROM qc_h5_interaction_pattern"):
            return "trusted"
        raise AssertionError(f"未预期的 SQL：{normalized}")

    async def fetchrow(self, *_: object) -> object:
        raise AssertionError("幂等重放不得重新汇总计数")


class _ReplayPool:
    def __init__(self, connection: _ReplayConnection) -> None:
        self._connection = connection

    def acquire(self) -> _AsyncContext:
        return _AsyncContext(self._connection)


class _RegisterConnection:
    def __init__(self) -> None:
        self.insert_arguments: tuple[object, ...] | None = None

    def transaction(self) -> _AsyncContext:
        return _AsyncContext(self)

    async def fetchrow(self, statement: str, *arguments: object) -> dict[str, str]:
        assert "VALUES ($1, $2, $3::jsonb)" in " ".join(statement.split())
        self.insert_arguments = arguments
        return {"status": "candidate"}

    async def fetch(self, *_: object) -> list[dict[str, str]]:
        assert self.insert_arguments is not None
        return [
            {
                "pattern_signature": str(self.insert_arguments[0]),
                "status": "candidate",
            }
        ]


@pytest.mark.asyncio
async def test_duplicate_observation_returns_existing_status_without_recounting() -> None:
    connection = _ReplayConnection()
    repository = PostgresH5InteractionPatternRepository(
        pool=_ReplayPool(connection),
    )

    status = await repository.record_outcome(
        pattern_signature="a" * 64,
        task_id="11111111-1111-1111-1111-111111111111",
        page_capture_id="22222222-2222-2222-2222-222222222222",
        outcome="failure",
        reason_code="replayed",
    )

    assert status == "trusted"
    assert not any(
        statement.startswith("UPDATE qc_h5_interaction_pattern")
        for statement in connection.statements
    )


@pytest.mark.asyncio
async def test_register_discovered_persists_the_detected_pattern_kind() -> None:
    connection = _RegisterConnection()
    repository = PostgresH5InteractionPatternRepository(
        pool=_ReplayPool(connection),
    )
    pattern = H5AdaptiveNavigationPattern(
        signature="b" * 64,
        pattern_kind="page_tab_navigation",
        pattern_payload={
            "schema_version": "h5-interaction-pattern.v1",
            "pattern_kind": "page_tab_navigation",
        },
        items=(
            H5AdaptiveNavigationItem(
                index=1,
                label="推荐",
                selector="#tab-1",
                active=True,
            ),
        ),
    )

    statuses = await repository.register_discovered(patterns=(pattern,))

    assert statuses == {pattern.signature: "candidate"}
    assert connection.insert_arguments is not None
    assert connection.insert_arguments[1] == "page_tab_navigation"
