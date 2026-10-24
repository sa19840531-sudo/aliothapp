"""Web 纯探索孤儿的失联收束；不生成正式质检结果或伪造会话恢复。"""

from services.api.app.domain.exploration.web_runtime_contract import CURRENT_WEB_RUNTIME_VERSION
from services.api.app.infrastructure.db.page_graph_repository import PostgresPageGraphRepository
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresWebExplorationOrphanRepository:
    def __init__(self, *, pool=None):
        self._pool = pool

    async def settle(self, *, task_id: str, run_id: str) -> bool:
        """调用者已验证 Worker 失联；PG 租约尚有效时仍拒绝终态化。"""
        pool = self._pool or await get_postgres_pool()
        session_id = f"web:{run_id}"
        async with pool.acquire() as connection, connection.transaction():
            identity = await connection.fetchrow(
                """SELECT task.finalization_epoch FROM qc_agent_run AS run
                   JOIN qc_task AS task ON task.id=run.task_id
                   WHERE run.id=$1::uuid AND task.id=$2::uuid
                     AND task.target_platform='web' AND run.agent_type='main'
                     AND run.status='running' AND task.status='running'
                     AND task.finalization_state <> 'sealed'
                     AND COALESCE(run.output_context->>'cancel_requested','false') <> 'true'
                     AND run.input_context->'task_config'->>'web_runtime_version'=$3
                     AND run.input_context->'task_config'->>'execution_mode'='exploration_only'
                     AND run.id=(SELECT id FROM qc_agent_run WHERE task_id=task.id
                         AND agent_type='main' ORDER BY created_at DESC,id DESC LIMIT 1)
                   FOR NO KEY UPDATE OF run, task""",
                run_id,
                task_id,
                CURRENT_WEB_RUNTIME_VERSION,
            )
            if identity is None:
                return False
            cursors = await connection.fetch(
                "SELECT lease_expires_at FROM qc_web_run_stage_checkpoint "
                "WHERE run_id=$1::uuid AND stage_status='running' FOR UPDATE",
                run_id,
            )
            session = await connection.fetchrow(
                "SELECT lease_expires_at FROM qc_surface_session "
                "WHERE task_id=$1::uuid AND session_id=$2 "
                "AND surface_kind='web' AND session_owner='web-v2-workflow' FOR UPDATE",
                task_id,
                session_id,
            )
            now = await connection.fetchval("SELECT clock_timestamp()")
            if any(
                row["lease_expires_at"] and row["lease_expires_at"] > now
                for row in [*cursors, *([session] if session else [])]
            ):
                return False
            await connection.execute(
                """UPDATE qc_action_execution SET
                       status=CASE WHEN status='evidence_pending' THEN 'evidence_failed'
                                   ELSE 'failed' END,
                       error_code='WEB_WORKER_ORPHANED',
                       error_message='Web Worker 失联且租约过期，浏览器上下文已丢失',
                       action_finished_at=COALESCE(action_finished_at,clock_timestamp()),
                       evidence_finished_at=CASE WHEN status='evidence_pending'
                            THEN clock_timestamp() ELSE evidence_finished_at END,
                       row_version=row_version+1,updated_at=clock_timestamp()
                   WHERE task_id=$1::uuid AND session_id=$2 AND finalization_epoch=$3
                     AND status IN ('planned','executing','evidence_pending')""",
                task_id,
                session_id,
                identity["finalization_epoch"],
            )
            await connection.execute(
                """UPDATE qc_surface_session SET status='lost',lease_owner=NULL,
                       lease_token=NULL,lease_expires_at=NULL,current_surface_ref=NULL,
                       failure_reason='WEB_WORKER_ORPHANED',lost_at=clock_timestamp(),
                       row_version=row_version+1,updated_at=clock_timestamp()
                   WHERE task_id=$1::uuid AND session_id=$2
                     AND surface_kind='web' AND session_owner='web-v2-workflow'
                     AND status IN ('starting','active','draining','closing')""",
                task_id,
                session_id,
            )
            await connection.execute(
                """UPDATE qc_surface_ref SET status='lost',pin_reason=NULL,
                       released_at=clock_timestamp(),row_version=row_version+1,
                       updated_at=clock_timestamp()
                   WHERE task_id=$1::uuid AND session_id=$2
                     AND status NOT IN ('released','lost')""",
                task_id,
                session_id,
            )
            # cancelled 仅表示遗留阶段停止执行；Run 仍按基础设施失败收口，绝不标成功。
            await connection.execute(
                """UPDATE qc_web_run_stage_checkpoint SET stage_status='cancelled',
                       next_attempt_at=NULL,last_error_code='WEB_WORKER_ORPHANED',
                       claimed_worker_id=NULL,claim_token=NULL,claimed_at=NULL,
                       lease_expires_at=NULL,row_version=row_version+1,
                       updated_at=clock_timestamp()
                   WHERE run_id=$1::uuid AND stage_status <> 'cancelled'
                     AND NOT (stage_status IN ('succeeded','gapped')
                              AND current_stage IN ('session_close','ad_closeout'))""",
                run_id,
            )
        # 动作状态落盘后重建现有投影；失败时保留 Run running，由下次幂等收束重试。
        await PostgresPageGraphRepository(pool=pool).rebuild_task(task_id=task_id)
        return True
