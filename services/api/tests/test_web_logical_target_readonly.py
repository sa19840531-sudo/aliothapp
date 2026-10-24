"""读取别名不得争抢 Run/Session 写锁，身份围栏仍必须成立。"""

from contextlib import asynccontextmanager

import pytest

from services.api.app.infrastructure.db.web_logical_target_repository import (
    PostgresWebLogicalTargetRepository,
)
from services.api.tests.test_web_run_policy_snapshot import snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_scope", [True, False])
async def test_find_uses_readonly_snapshot_without_row_locks(valid_scope):
    frozen = snapshot()

    class Connection:
        options = None

        @asynccontextmanager
        async def transaction(self, **options):
            self.options = options
            yield

        async def fetchrow(self, query, *args):
            # 等价于只读事务拒绝行写锁；跨页查询不应等待正在持锁的发布事务。
            if "FOR UPDATE" in query:
                raise RuntimeError("只读查询错误地申请行锁")
            if "SELECT task.finalization_epoch" in query:
                assert args == ("task-a", "run-a", "web:run-a", "capture-a", True)
                return {"finalization_epoch": 0} if valid_scope else None
            if "qc_web_run_policy_snapshot" in query:
                return {"task_id": "task-a", "agent_run_id": "run-a",
                        "payload": frozen.to_payload(), "payload_sha256": frozen.payload_sha256}
            if "qc_web_logical_target" in query:
                return None
            raise AssertionError(query)

        async def fetchval(self, query, *args):
            assert "qc_web_logical_target_alias" in query
            return None

    connection = Connection()

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield connection

    repository = PostgresWebLogicalTargetRepository(pool=Pool())
    kwargs = dict(task_id="task-a", agent_run_id="run-a", session_id="web:run-a",
                  source_capture_id="capture-a", url="https://example.test/")
    if valid_scope:
        assert await repository.find(**kwargs) is None
        assert connection.options == {"isolation": "repeatable_read", "readonly": True}
    else:
        with pytest.raises(ValueError, match="WEB_LOGICAL_TARGET_SCOPE_INVALID"):
            await repository.find(**kwargs)
