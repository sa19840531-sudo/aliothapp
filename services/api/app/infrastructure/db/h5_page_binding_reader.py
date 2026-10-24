"""H5 原页面输入的当前消费授权，不借用 Web 序列权限。"""

from contextlib import asynccontextmanager

from services.api.app.application.inspection_work_items import (
    InspectionWorkItemValidationError,
    build_inspection_attempt_input_hash,
)
from services.api.app.application.observation_packet import ObservationPacketValidationError
from services.api.app.infrastructure.db.h5_producer_result_reuse import read_verified_page_input
from services.api.app.infrastructure.db.pool import get_postgres_pool


@asynccontextmanager
async def _connection_scope(pool, connection):
    if connection is not None:
        yield connection
    else:
        async with pool.acquire() as acquired:
            yield acquired


class PostgresH5PageBindingReader:
    def __init__(self, *, pool=None, database_url=None):
        self._pool = pool
        self._database_url = database_url

    async def load_authorized(self, **identity):
        inputs, _binding = await self.load_authorized_bundle(**identity)
        return inputs

    async def load_authorized_bundle(
        self,
        *,
        task_id,
        page_capture_id,
        source_agent_run_id,
        input_set_id,
        rule_id,
        rule_contract_hash,
        work_item_id=None,
        attempt_id=None,
        consumer_agent_run_id=None,
        lease_token=None,
        connection=None,
    ):
        if not all((work_item_id, attempt_id, consumer_agent_run_id, lease_token)):
            raise ObservationPacketValidationError("H5 页面读取缺少当前消费租约")
        from services.api.app.infrastructure.db.inspection_work_item_repository import (
            _load_rule_evidence_bindings,
        )

        pool = self._pool or (
            await get_postgres_pool(self._database_url) if connection is None else None
        )
        async with _connection_scope(pool, connection) as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{task_id}"
            )
            row = await connection.fetchrow(
                """
                SELECT work.input_hash, work.finalization_epoch, work.ruleset_snapshot_hash,
                       work.producer_agent_run_id::text, work.page_capture_id::text,
                       attempt.input_hash AS attempt_input_hash, member.evidence_input_set_id,
                       member.rule_code
                FROM qc_inspection_work_item work
                JOIN qc_task task ON task.id=work.task_id
                JOIN qc_inspection_work_item_rule member ON member.work_item_id=work.id
                JOIN qc_inspection_attempt attempt ON attempt.work_item_id=work.id
                    AND attempt.attempt_no=work.attempt_count
                    AND attempt.lease_token=work.lease_token
                    AND attempt.consumer_agent_run_id=work.consumer_agent_run_id
                WHERE work.id=$1::uuid AND work.task_id=$2::uuid AND task.target_platform='h5'
                    AND task.status IN ('running','review')
                    AND task.finalization_epoch=work.finalization_epoch
                    AND task.finalization_state IN (
                        'open','exploration_draining','session_draining')
                    AND work.status='running' AND work.control_mode='enforced'
                    AND work.lease_token=$3::uuid AND work.lease_expires_at>now()
                    AND work.consumer_agent_run_id=$4::uuid
                    AND member.rule_id=$5::uuid AND member.contract_hash=$6
                    AND member.status IN ('pending','judged')
                    AND member.evidence_input_set_id IS NOT NULL
                    AND attempt.id=$7::uuid AND attempt.status='running'
                FOR UPDATE OF work, member, attempt
                """,
                work_item_id,
                task_id,
                lease_token,
                consumer_agent_run_id,
                rule_id,
                rule_contract_hash,
                attempt_id,
            )
            if row is None:
                raise ObservationPacketValidationError("H5 原输入没有当前 Work/周期/租约授权")
            try:
                bindings = await _load_rule_evidence_bindings(connection, work_item_id=work_item_id)
            except InspectionWorkItemValidationError as exc:
                # 保留证据校验类型，在模型调用前拒绝，不放行缺失或已降级的旧帧。
                raise ObservationPacketValidationError(str(exc)) from exc
            binding = next((item for item in bindings if item.rule_id == rule_id), None)
            effective = binding.evidence_input_set_id if binding else row["evidence_input_set_id"]
            producer = binding.producer_agent_run_id if binding else row["producer_agent_run_id"]
            roles = (
                (dict(binding.evidence_capture_roles) or {"primary": binding.page_capture_id})
                if binding
                else {"primary": row["page_capture_id"]}
            )
            if (
                effective != input_set_id
                or source_agent_run_id != producer
                or page_capture_id not in roles.values()
                or build_inspection_attempt_input_hash(
                    work_item_input_hash=row["input_hash"],
                    rule_evidence=bindings,
                )
                != row["attempt_input_hash"]
            ):
                raise ObservationPacketValidationError("H5 Attempt 消费输入已漂移")
            inputs = await read_verified_page_input(
                connection,
                input_set_id=input_set_id,
                packet_runs=(source_agent_run_id, consumer_agent_run_id),
            )
            if inputs is None or any(
                (
                    inputs.task_id != task_id,
                    inputs.source_agent_run_id != source_agent_run_id,
                    inputs.rule_id != rule_id,
                    inputs.rule_code != row["rule_code"],
                    inputs.rule_contract_hash != rule_contract_hash,
                    inputs.finalization_epoch != row["finalization_epoch"],
                    inputs.ruleset_snapshot_hash != row["ruleset_snapshot_hash"],
                    len(inputs.eligible_capture_ids) != 1,
                    inputs.eligible_capture_ids[0] not in roles.values(),
                )
            ):
                raise ObservationPacketValidationError("H5 原页面 InputSet 证据闭包不匹配")
            return inputs, binding
