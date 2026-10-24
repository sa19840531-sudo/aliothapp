"""H5 预期动作证据库存的 PostgreSQL 读写仓储。"""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from services.api.app.application.h5_evidence_inventory import (
    H5EvidenceInventoryItem,
    H5EvidenceInventorySnapshot,
    H5EvidenceInventoryStatus,
)
from services.api.app.infrastructure.db.finalization_recovery_fence import (
    assert_finalization_recovery_fence,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


async def _upsert_snapshot(
    connection: Any,
    *,
    item_id: str,
    payload_hash: str,
    payload: str,
    payload_values: dict[str, Any],
) -> Any:
    row = await connection.fetchrow(_UPSERT_SQL, item_id, payload_hash, payload)
    if row is None:
        row = await connection.fetchrow(
            _CURRENT_SNAPSHOT_SQL,
            payload_values["action_execution_id"],
            payload_values["task_id"],
            payload_values["agent_run_id"],
            payload_values["session_id"],
        )
    return row


class PostgresH5EvidenceInventoryRepository:
    """从 Action 左连接取数，并以动作身份幂等保存库存项。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def upsert_item(
        self,
        *,
        item: dict[str, Any],
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> H5EvidenceInventorySnapshot:
        """逐项更新且 published 数只增不减，允许并发重复 handoff。"""
        payload_values = _canonicalize({
            key: value
            for key, value in item.items()
            if key
            not in {
                "payload_hash",
                "row_version",
                "published_inspection_count",
                "verified_at",
            }
        })
        payload = json.dumps(
            payload_values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        item_id = f"h5-evidence:{item['action_execution_id']}"
        payload_hash = hashlib.sha256(payload.encode()).hexdigest()
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            if recovery_lease_token is None:
                row = await _upsert_snapshot(
                    connection,
                    item_id=item_id,
                    payload_hash=payload_hash,
                    payload=payload,
                    payload_values=payload_values,
                )
            else:
                async with connection.transaction():
                    await assert_finalization_recovery_fence(
                        connection,
                        task_id=str(item["task_id"]),
                        recovery_lease_token=recovery_lease_token,
                        recovery_generation=recovery_generation,
                        recovery_lease_expires_at=recovery_lease_expires_at,
                    )
                    row = await _upsert_snapshot(
                        connection,
                        item_id=item_id,
                        payload_hash=payload_hash,
                        payload=payload,
                        payload_values=payload_values,
                    )
        if row is None:
            raise RuntimeError("H5 证据库存自然身份冲突")
        return _snapshot(row)

    async def upsert_inventory_item(
        self,
        *,
        item: H5EvidenceInventoryItem,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> H5EvidenceInventorySnapshot:
        """应用层端口：仅接受不可变库存项并用规范 JSON 计算重放身份。"""
        values = {
            "task_id": item.task_id,
            "agent_run_id": item.agent_run_id,
            "session_id": item.session_id,
            "action_execution_id": item.action_execution_id,
            "before_capture_id": item.before_capture_id,
            "after_capture_id": item.after_capture_id,
            "page_type": item.page_type,
            "page_state": item.page_state,
            "verification_summary": dict(item.verification_summary),
            "before_manifest_id": item.before_manifest_id,
            "before_manifest_hash": item.before_manifest_hash,
            "after_manifest_id": item.after_manifest_id,
            "after_manifest_hash": item.after_manifest_hash,
            "authority_source": item.authority_source,
        }
        values["status"] = item.status.value
        values["reason_codes"] = list(item.reason_codes)
        values["required_artifacts"] = list(item.required_artifacts)
        values["found_artifacts"] = list(item.found_artifacts)
        return await self.upsert_item(
            item=values,
            recovery_lease_token=recovery_lease_token,
            recovery_generation=recovery_generation,
            recovery_lease_expires_at=recovery_lease_expires_at,
        )

    async def mark_published(
        self, *, action_execution_id: str, published_count: int
    ) -> H5EvidenceInventorySnapshot:
        """发布成功后单调记账；重放不会降低已发布数。"""
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_h5_evidence_inventory_item
                SET published_inspection_count = GREATEST(
                        published_inspection_count, $2::integer
                    ), updated_at = now()
                WHERE action_execution_id = $1
                RETURNING payload_hash, row_version, published_inspection_count, status
                """,
                action_execution_id,
                published_count,
            )
        if row is None:
            raise RuntimeError("H5 证据库存发布记账身份冲突")
        return _snapshot(row)


def _snapshot(row: Any) -> H5EvidenceInventorySnapshot:
    return H5EvidenceInventorySnapshot(
        payload_hash=str(row["payload_hash"]),
        row_version=int(row["row_version"]),
        published_inspection_count=int(row["published_inspection_count"]),
        status=H5EvidenceInventoryStatus(str(row["status"])),
    )


def _canonicalize(value: Any) -> Any:
    """将集合语义的嵌套序列规范化，拒绝无法严格 JSON 编码的值。"""
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("H5 证据库存 JSON 字典键必须为字符串")
        return {key: _canonicalize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        normalized = [_canonicalize(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
    return value


_UPSERT_SQL = """
WITH changed AS (
INSERT INTO qc_h5_evidence_inventory_item (inventory_item_id, task_id, agent_run_id, session_id, action_execution_id, before_capture_id, after_capture_id, page_type, page_state, status, reason_codes, required_artifacts, found_artifacts, verification_summary, before_manifest_id, before_manifest_hash, after_manifest_id, after_manifest_hash, authority_source, schema_version, payload_hash)
VALUES ($1, ($3::jsonb->>'task_id')::uuid, ($3::jsonb->>'agent_run_id')::uuid, $3::jsonb->>'session_id', $3::jsonb->>'action_execution_id', NULLIF($3::jsonb->>'before_capture_id','')::uuid, NULLIF($3::jsonb->>'after_capture_id','')::uuid, $3::jsonb->>'page_type', $3::jsonb->>'page_state', $3::jsonb->>'status', COALESCE($3::jsonb->'reason_codes','[]'::jsonb), COALESCE($3::jsonb->'required_artifacts','[]'::jsonb), COALESCE($3::jsonb->'found_artifacts','[]'::jsonb), COALESCE($3::jsonb->'verification_summary','{}'::jsonb), NULLIF($3::jsonb->>'before_manifest_id',''), NULLIF($3::jsonb->>'before_manifest_hash',''), NULLIF($3::jsonb->>'after_manifest_id',''), NULLIF($3::jsonb->>'after_manifest_hash',''), COALESCE($3::jsonb->>'authority_source','legacy_object_verified'), 'h5-evidence-inventory/v1', $2)
ON CONFLICT (action_execution_id) DO UPDATE SET before_capture_id = EXCLUDED.before_capture_id, after_capture_id = EXCLUDED.after_capture_id, page_type = EXCLUDED.page_type, page_state = EXCLUDED.page_state, status = EXCLUDED.status, reason_codes = EXCLUDED.reason_codes, required_artifacts = EXCLUDED.required_artifacts, found_artifacts = EXCLUDED.found_artifacts, verification_summary = EXCLUDED.verification_summary, before_manifest_id = EXCLUDED.before_manifest_id, before_manifest_hash = EXCLUDED.before_manifest_hash, after_manifest_id = EXCLUDED.after_manifest_id, after_manifest_hash = EXCLUDED.after_manifest_hash, authority_source = EXCLUDED.authority_source, payload_hash = EXCLUDED.payload_hash, row_version = qc_h5_evidence_inventory_item.row_version + 1, published_inspection_count = GREATEST(qc_h5_evidence_inventory_item.published_inspection_count, EXCLUDED.published_inspection_count), verified_at = now(), updated_at = now() WHERE qc_h5_evidence_inventory_item.task_id = EXCLUDED.task_id AND qc_h5_evidence_inventory_item.agent_run_id = EXCLUDED.agent_run_id AND qc_h5_evidence_inventory_item.session_id = EXCLUDED.session_id AND qc_h5_evidence_inventory_item.status <> 'failed' AND qc_h5_evidence_inventory_item.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash RETURNING payload_hash, row_version, published_inspection_count, status
), current AS (
SELECT payload_hash, row_version, published_inspection_count, status
FROM qc_h5_evidence_inventory_item
WHERE action_execution_id = ($3::jsonb->>'action_execution_id')
  AND task_id = ($3::jsonb->>'task_id')::uuid
  AND agent_run_id = ($3::jsonb->>'agent_run_id')::uuid
  AND session_id = $3::jsonb->>'session_id'
)
SELECT payload_hash, row_version, published_inspection_count, status FROM changed
UNION ALL
SELECT payload_hash, row_version, published_inspection_count, status FROM current
WHERE NOT EXISTS (SELECT 1 FROM changed)
"""


_CURRENT_SNAPSHOT_SQL = """
SELECT payload_hash, row_version, published_inspection_count, status
FROM qc_h5_evidence_inventory_item
WHERE action_execution_id = $1
  AND task_id = $2::uuid
  AND agent_run_id = $3::uuid
  AND session_id = $4
"""
