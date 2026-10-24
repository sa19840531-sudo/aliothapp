"""整段 H5 旅程使用独立连接的 PostgreSQL 锁，不占用业务连接池。"""

import asyncio
import json
import os
from contextlib import asynccontextmanager

import asyncpg

from services.api.app.application.h5_recapture_lease import retain_h5_pending_operation
from services.api.app.application.h5_session_journey import (
    H5SessionJourneyGate,
    H5SessionJourneyLost,
)

H5_JOURNEY_LOCK_PREFIX = "h5-session-journey:"
H5_JOURNEY_ACK_TIMEOUT_SECONDS = 5


class PostgresH5SessionJourneyGate(H5SessionJourneyGate):
    def __init__(self, *, delegate, database_url=None):
        self._database_url = database_url
        self._control_slots = asyncio.Semaphore(16)
        super().__init__(delegate=delegate, remote_hold=self._hold_postgres)

    @asynccontextmanager
    async def _hold_postgres(self, scope):
        if scope.try_only and self._control_slots.locked():
            raise H5SessionJourneyLost("H5 控制连接容量占满，嵌套恢复不得交叉等待")
        async with self._control_slots:
            async with self._hold_postgres_connection(scope):
                yield

    @asynccontextmanager
    async def _hold_postgres_connection(self, scope):
        url = self._database_url or os.getenv("ALIOTH_DATABASE_URL")
        if not url:
            raise RuntimeError("H5 旅程缺少数据库配置，不能退回进程内互斥")
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
        # 独立控制连接避免多个旅程占满业务池后，内部命令无法取得连接。
        connection = await asyncpg.connect(url, timeout=10, statement_cache_size=0)
        key = H5_JOURNEY_LOCK_PREFIX + scope.session_id
        token = scope.token
        reserved = False
        def lost(_):
            scope.invalidate()
        connection.add_termination_listener(lost)
        try:
            if scope.try_only:
                acquired = await connection.fetchval(
                    "SELECT pg_try_advisory_lock(hashtextextended($1,0))", key,
                )
                if not acquired:
                    raise H5SessionJourneyLost("H5 恢复目标旅程被占用，拒绝跨进程交叉等待")
            else:
                await connection.execute("SELECT pg_advisory_lock(hashtextextended($1,0))", key)
            scope.check(scope.task_id)
            try:
                await connection.execute(
                    "INSERT INTO qc_h5_session_journey_guard(token,task_id,session_id,status) "
                    "VALUES($1::uuid,$2,$3,'active')", token, scope.task_id, scope.session_id,
                )
                reserved = True
            except asyncpg.UniqueViolationError:
                raise H5SessionJourneyLost("H5 会话已有未排空或未确认的物理旅程") from None
            yield
        finally:
            async def finish_guard():
                # 连接锁会自动释放；这条记录只有可靠退出才可标记排空。
                control = connection
                if connection.is_closed():
                    control = await asyncpg.connect(url, timeout=5, statement_cache_size=0)
                try:
                    updated = await control.execute(
                        "UPDATE qc_h5_session_journey_guard SET status=$2::varchar,reason_code=$3,"
                        "drained_at=CASE WHEN $2::varchar='drained' THEN now() ELSE NULL END "
                        "WHERE token=$1::uuid AND task_id=$4 AND status='active'",
                        token, "quarantined" if scope.quarantine_reason else "drained",
                        scope.quarantine_reason, scope.task_id,
                    )
                    if reserved and updated != "UPDATE 1":
                        raise H5SessionJourneyLost("H5 旅程排空写回围栏失效，停止事实未确认")
                finally:
                    if control is not connection:
                        await control.close(timeout=2)

            async def bounded_finish():
                try:
                    async with asyncio.timeout(H5_JOURNEY_ACK_TIMEOUT_SECONDS):
                        await finish_guard()
                except BaseException:
                    scope.quarantine_reason = "H5_JOURNEY_DRAIN_ACK_UNCONFIRMED"
                    raise

            cleanup = asyncio.create_task(bounded_finish())
            scope.operations.add(cleanup)
            cleanup.add_done_callback(scope.operations.discard)
            cancelled = False
            deadline = asyncio.get_running_loop().time() + H5_JOURNEY_ACK_TIMEOUT_SECONDS
            try:
                while not cleanup.done():
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        scope.quarantine_reason = "H5_JOURNEY_DRAIN_ACK_UNCONFIRMED"
                        if not cleanup.cancelling():
                            cleanup.cancel()
                        retain_h5_pending_operation(cleanup)
                        connection.terminate()
                        raise H5SessionJourneyLost("H5 排空确认写回未确认，保留隔离事实")
                    try:
                        await asyncio.wait({cleanup}, timeout=remaining)
                    except asyncio.CancelledError:
                        cancelled = cancelled or not cleanup.done()
                        continue
                await cleanup
            finally:
                # 结束控制连接即释放锁；移除监听，正常退出不能被误报为丢租。
                connection.remove_termination_listener(lost)
                try:
                    async with asyncio.timeout(5):
                        await connection.close(timeout=5)
                except BaseException:
                    connection.terminate()
                    raise
            if cancelled:
                raise asyncio.CancelledError

    async def _ack_physical_stop(self, scope, proofs):
        async def ack():
            url = (self._database_url or os.environ["ALIOTH_DATABASE_URL"]).replace(
                "postgresql+asyncpg://", "postgresql://", 1,
            )
            async with self._control_slots:
                connection = await asyncpg.connect(url, timeout=5, statement_cache_size=0)
                try:
                    async with connection.transaction():
                        await connection.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                            H5_JOURNEY_LOCK_PREFIX + scope.session_id,
                        )
                        payload = json.dumps(proofs)
                        updated = await connection.execute(
                            "UPDATE qc_h5_session_journey_guard "
                            "SET status='drained',drained_at=now(),"
                            "physical_stop_proof=$4::jsonb "
                            "WHERE token=$1::uuid AND task_id=$2 AND session_id=$3 "
                            "AND status IN ('active','quarantined')",
                            scope.token, scope.task_id, scope.session_id, payload,
                        )
                        if updated != "UPDATE 1" and not await connection.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM qc_h5_session_journey_guard "
                            "WHERE token=$1::uuid AND task_id=$2 AND session_id=$3 "
                            "AND status='drained' "
                            "AND physical_stop_proof=$4::jsonb)",
                            scope.token, scope.task_id, scope.session_id, payload,
                        ):
                            raise H5SessionJourneyLost("H5 停止证明不属于原隔离记录，不能解封")
                finally:
                    await connection.close(timeout=2)

        task = asyncio.create_task(ack())
        scope.operations.add(task)
        task.add_done_callback(scope.operations.discard)
        try:
            done, _ = await asyncio.wait({task}, timeout=H5_JOURNEY_ACK_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            retain_h5_pending_operation(task)
            raise
        if not done:
            task.cancel()
            retain_h5_pending_operation(task)
            raise H5SessionJourneyLost("H5 停止证明写回未确认，继续隔离")
        await task
