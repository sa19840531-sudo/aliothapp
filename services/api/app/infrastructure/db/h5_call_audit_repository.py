"""同任务全部消费者调用流水及冻结预算的只读一致快照。"""

from services.api.app.application.h5_call_audit import summarize_h5_calls
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresH5CallAuditRepository:
    def __init__(self, *, pool=None):
        self._pool = pool

    async def read(self, *, task_id, access_scope):
        pool = self._pool or await get_postgres_pool()
        async with (
            pool.acquire() as connection,
            connection.transaction(isolation="repeatable_read", readonly=True),
        ):
            task = await connection.fetchrow(
                """SELECT task.id::text AS task_id, task.status, task.finalization_epoch
                   FROM qc_task AS task WHERE task.id=$1::uuid AND task.target_platform='h5'
                     AND ($2::boolean OR (task.tenant_id=$3::uuid AND (
                         $4::boolean OR task.submitter_id=$5::uuid
                         OR task.department_name=$6::varchar)))
                """,
                task_id,
                access_scope.can_read_any,
                access_scope.tenant_id,
                access_scope.can_read_tenant,
                access_scope.actor_user_id,
                access_scope.department_scope,
            )
            if task is None:
                return None
            budgets = await connection.fetch(
                """SELECT task_id::text, agent_run_id::text, finalization_epoch, max_calls,
                          admitted_calls,deadline,created_at,updated_at
                   FROM qc_h5_llm_call_budget WHERE task_id=$1::uuid ORDER BY finalization_epoch""",
                task_id,
            )
            rows = await connection.fetch(
                """SELECT id::text AS event_id,task_id::text,agent_run_id::text,source,operation,
                          provider,model,usage_available,ai_calls,total_tokens,
                          total_cost::text, cost_currency,pricing_configured,created_at,
                          input_tokens,output_tokens,cache_hit_tokens,billable_input_tokens,
                          CASE WHEN metadata->>'http_status_code' ~ '^[1-5][0-9]{2}$'
                               THEN (metadata->>'http_status_code')::integer
                               END AS http_status_code,
                          CASE WHEN metadata->>'request_status' IN ('succeeded','failed')
                               THEN metadata->>'request_status' END AS request_status,
                          CASE WHEN jsonb_typeof(metadata->'physical_request')='boolean'
                               THEN (metadata->>'physical_request')::boolean END AS physical_request
                   FROM qc_llm_usage_event WHERE task_id=$1::uuid ORDER BY created_at,id""",
                task_id,
            )
        # 初始化累计同任务调用；旧 Epoch 行仍完整保留，不把已封存 Epoch 当不存在。
        budget = dict(budgets[-1]) if budgets else None
        return {
            "task": dict(task),
            **summarize_h5_calls(rows, budget=budget),
            "budget_history": [dict(row) for row in budgets],
            "usage_events": [dict(row) for row in rows],
        }
