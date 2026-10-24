"""旧运行恢复查询不得混入当前、已取消或其他平台运行。"""

from services.api.app.infrastructure.queue.state_store import PostgresAgentRunStateStore
from services.api.tests.test_agent_run_state_store import FakePool


async def test_postgres_legacy_recovery_query_is_bounded_and_excludes_current_and_cancel():
    pool = FakePool()
    store = PostgresAgentRunStateStore(pool=pool)
    assert await store.list_legacy_web_recovery_records(limit=25) == []
    query, args = pool.connection.calls[0]
    assert "status IN ('pending', 'running')" in query
    assert "input_context->'task_config'->'_task_target'->>'platform' = 'web'" in query
    assert "web_runtime_version' IS DISTINCT FROM $1" in query
    assert "output_context->>'cancel_requested'" in query
    assert "LIMIT $2" in query
    assert args == ("web-runtime.v2", 25)
    assert await store.list_legacy_web_recovery_records(limit=0) == []
    assert len(pool.connection.calls) == 1
