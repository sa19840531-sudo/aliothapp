"""H5 库存仓储返回持久化快照及幂等语义的合同测试。"""

from copy import deepcopy

import pytest

from services.api.app.infrastructure.db.h5_evidence_inventory_repository import (
    PostgresH5EvidenceInventoryRepository,
)


class _Acquire:
    def __init__(self, connection) -> None:
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, *args) -> bool:
        return False


class _Pool:
    def __init__(self, connection) -> None:
        self._connection = connection

    def acquire(self):
        return _Acquire(self._connection)


def _item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "session_id": "h5:run-1",
        "action_execution_id": "action-1",
        "before_capture_id": "before-1",
        "after_capture_id": "after-1",
        "status": "pass",
        "reason_codes": [],
        "required_artifacts": ["after:dom", "before:screenshot"],
        "found_artifacts": ["after:dom", "before:screenshot"],
        "verification_summary": {"artifacts": [{"artifact_id": "a"}]},
    }
    item.update(overrides)
    return item


class _Connection:
    """以数据库返回快照驱动的 fake，保留 SQL/参数以验证仓储协议。"""

    def __init__(self, rows: list[dict[str, object] | None]) -> None:
        self._rows = iter(rows)
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, sql: str, *args: object):
        self.calls.append((sql, args))
        return next(self._rows)


@pytest.mark.asyncio
async def test_same_hash_returns_existing_snapshot_without_row_version_increment() -> None:
    connection = _Connection(
        [
            {
                "payload_hash": "same",
                "row_version": 4,
                "published_inspection_count": 1,
                "status": "pass",
            },
            {
                "payload_hash": "same",
                "row_version": 4,
                "published_inspection_count": 1,
                "status": "pass",
            },
        ]
    )
    repository = PostgresH5EvidenceInventoryRepository(pool=_Pool(connection))

    first = await repository.upsert_item(item=_item())
    second = await repository.upsert_item(item=_item())

    assert (first.payload_hash, first.row_version) == ("same", 4)
    assert (second.payload_hash, second.row_version) == ("same", 4)
    sql = connection.calls[0][0]
    assert "WITH changed AS" in sql
    assert "payload_hash IS DISTINCT FROM EXCLUDED.payload_hash" in sql
    assert "WHERE NOT EXISTS (SELECT 1 FROM changed)" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pass", "insufficient", "failed"])
async def test_upsert_binds_canonical_json_text_for_each_inventory_status(status: str) -> None:
    """asyncpg JSONB 参数必须是严格 JSON 文本，而非 Python dict。"""
    connection = _Connection(
        [
            {
                "payload_hash": f"{status}-hash",
                "row_version": 1,
                "published_inspection_count": 0,
                "status": status,
            }
        ]
    )
    repository = PostgresH5EvidenceInventoryRepository(pool=_Pool(connection))

    await repository.upsert_item(item=_item(status=status))

    payload = connection.calls[0][1][2]
    assert isinstance(payload, str)
    assert '"status":"' + status + '"' in payload


@pytest.mark.asyncio
async def test_changed_hash_returns_incremented_persisted_snapshot() -> None:
    connection = _Connection(
        [
            {
                "payload_hash": "changed",
                "row_version": 5,
                "published_inspection_count": 1,
                "status": "insufficient",
            }
        ]
    )
    repository = PostgresH5EvidenceInventoryRepository(pool=_Pool(connection))

    snapshot = await repository.upsert_item(
        item=_item(status="insufficient", reason_codes=["after:dom_missing"])
    )

    assert snapshot.row_version == 5
    assert snapshot.status.value == "insufficient"
    assert "row_version = qc_h5_evidence_inventory_item.row_version + 1" in connection.calls[0][0]


@pytest.mark.asyncio
async def test_failed_item_is_sticky_and_returns_its_original_snapshot() -> None:
    connection = _Connection(
        [
            {
                "payload_hash": "failed-original",
                "row_version": 7,
                "published_inspection_count": 0,
                "status": "failed",
            }
        ]
    )
    repository = PostgresH5EvidenceInventoryRepository(pool=_Pool(connection))

    snapshot = await repository.upsert_item(item=_item(status="pass", reason_codes=[]))

    assert (snapshot.payload_hash, snapshot.row_version, snapshot.status.value) == (
        "failed-original",
        7,
        "failed",
    )
    assert "qc_h5_evidence_inventory_item.status <> 'failed'" in connection.calls[0][0]


@pytest.mark.asyncio
async def test_identity_conflict_and_mark_zero_row_fail_closed() -> None:
    repository = PostgresH5EvidenceInventoryRepository(pool=_Pool(_Connection([None, None, None])))

    with pytest.raises(RuntimeError, match="身份冲突"):
        await repository.upsert_item(item=_item())
    with pytest.raises(RuntimeError, match="身份冲突"):
        await repository.mark_published(action_execution_id="action-1", published_count=1)


@pytest.mark.asyncio
async def test_concurrent_first_write_reloads_winner_snapshot_after_cte_snapshot_miss() -> None:
    """并发首写的 loser 必须用新 statement 读取已提交 winner，而非误报冲突。"""

    class _ConcurrentFirstWriteConnection(_Connection):
        def __init__(self) -> None:
            super().__init__([])
            self._calls = 0

        async def fetchrow(self, sql: str, *args: object):
            self.calls.append((sql, args))
            self._calls += 1
            if self._calls == 1:
                return None
            return {
                "payload_hash": "winner",
                "row_version": 1,
                "published_inspection_count": 0,
                "status": "pass",
            }

    connection = _ConcurrentFirstWriteConnection()
    repository = PostgresH5EvidenceInventoryRepository(pool=_Pool(connection))

    snapshot = await repository.upsert_item(item=_item())

    assert (snapshot.payload_hash, snapshot.row_version) == ("winner", 1)
    assert len(connection.calls) == 2
    assert "FROM qc_h5_evidence_inventory_item" in connection.calls[1][0]
    assert connection.calls[1][1] == ("action-1", "task-1", "run-1", "h5:run-1")


@pytest.mark.asyncio
async def test_canonical_payload_hash_ignores_artifact_input_order_and_server_fields() -> None:
    rows = [
        {
            "payload_hash": "first",
            "row_version": 1,
            "published_inspection_count": 0,
            "status": "pass",
        },
        {
            "payload_hash": "first",
            "row_version": 1,
            "published_inspection_count": 0,
            "status": "pass",
        },
    ]
    connection = _Connection(rows)
    repository = PostgresH5EvidenceInventoryRepository(pool=_Pool(connection))
    first_item = _item(
        verification_summary={"artifacts": [{"artifact_id": "b"}, {"artifact_id": "a"}]},
        published_inspection_count=99,
        row_version=88,
    )
    second_item = deepcopy(first_item)
    second_item["verification_summary"] = {
        "artifacts": [{"artifact_id": "a"}, {"artifact_id": "b"}]
    }
    second_item["published_inspection_count"] = 0
    second_item["row_version"] = 1

    await repository.upsert_item(item=first_item)
    await repository.upsert_item(item=second_item)

    assert connection.calls[0][1][1] == connection.calls[1][1][1]
    assert "published_inspection_count" not in connection.calls[0][1][1]


@pytest.mark.asyncio
async def test_mark_published_returns_monotonic_snapshot() -> None:
    connection = _Connection(
        [
            {
                "payload_hash": "same",
                "row_version": 3,
                "published_inspection_count": 5,
                "status": "pass",
            }
        ]
    )
    repository = PostgresH5EvidenceInventoryRepository(pool=_Pool(connection))

    snapshot = await repository.mark_published(action_execution_id="action-1", published_count=2)

    assert snapshot.published_inspection_count == 5
    assert "GREATEST(" in connection.calls[0][0]
    assert (
        "RETURNING payload_hash, row_version, published_inspection_count, status"
        in connection.calls[0][0]
    )


@pytest.mark.asyncio
async def test_repository_rejects_non_json_inventory_payload() -> None:
    """canonical JSON 不得以 default=str 掩盖不可审计的值。"""
    connection = _Connection([])
    repository = PostgresH5EvidenceInventoryRepository(pool=_Pool(connection))

    with pytest.raises(TypeError):
        await repository.upsert_item(item=_item(verification_summary={"invalid": object()}))
    assert connection.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verification_summary",
    [
        {1: "non-string-key"},
        {"not-a-number": float("nan")},
        {"not-a-number": float("inf")},
    ],
)
async def test_repository_rejects_noncanonical_json_keys_and_numbers(
    verification_summary: dict[object, object],
) -> None:
    """字典键与浮点数必须是严格 JSON，不能静默改变审计事实。"""
    connection = _Connection([])
    repository = PostgresH5EvidenceInventoryRepository(pool=_Pool(connection))

    with pytest.raises((TypeError, ValueError)):
        await repository.upsert_item(item=_item(verification_summary=verification_summary))
    assert connection.calls == []
