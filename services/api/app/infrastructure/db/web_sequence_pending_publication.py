"""默认 Web 补扫仅消费既有序列 Publication，不重新采集或建立发布队列。"""

from uuid import UUID

from services.api.app.application.live_inspection_publication import (
    LiveInspectionPublicationBatchResult,
)
from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    PostgresH5InspectionPublicationRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresWebSequencePendingPublisher:
    def __init__(self, *, pool=None, database_url=None):
        self._pool = pool
        self._database_url = database_url

    async def publish_pending_batch(self, *, task_id, limit, cursor):
        if cursor is not None and not cursor.startswith("web-sequence:"):
            return None
        after = str(UUID(cursor.removeprefix("web-sequence:"))) if cursor else None
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT p.id::text,p.row_version FROM qc_h5_inspection_publication p "
                "JOIN qc_task t ON t.id=p.task_id WHERE p.task_id=$1::uuid AND t.target_platform='web' "
                "AND t.task_config->>'workflow_version'='v2' AND p.web_evidence_sequence_id IS NOT NULL "
                "AND (p.status='pending' OR (p.status='retry_wait' AND p.next_attempt_at<=now()) "
                "OR (p.status='leased' AND p.lease_expires_at<=now())) "
                "AND ($2::uuid IS NULL OR p.id>$2::uuid) ORDER BY p.id LIMIT $3",
                task_id,
                after,
                limit,
            )
        if not rows:
            return None
        repository = PostgresH5InspectionPublicationRepository(pool=pool)
        created = 0
        for row in rows:
            claim = await repository.claim(
                publication_id=row["id"],
                task_id=task_id,
                message_version=row["row_version"],
                lease_owner="web-sequence-closeout",
                lease_seconds=60,
            )
            if claim.lease is not None:
                created += len(await repository.process_web_sequence(lease=claim.lease))
        # 后续仍需原 stable 扫描，序列游标单独命名，不吞掉普通 Capture 游标。
        return LiveInspectionPublicationBatchResult(
            len(rows), created, True, "web-sequence:" + rows[-1]["id"]
        )
