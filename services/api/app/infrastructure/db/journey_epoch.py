"""报告只读已通过 Gate 锁定的证据周期，不授权旧周期继续执行。"""


async def closed_journey_epoch_is_bound(connection, *, task_id, agent_run_id, epoch):
    return bool(
        await connection.fetchval(
            "SELECT true FROM qc_task AS task JOIN qc_completion_gate_snapshot AS gate "
            "ON gate.gate_snapshot_id=task.completion_gate_snapshot_id AND gate.task_id=task.id "
            "AND gate.finalization_epoch=task.finalization_epoch AND gate.gate_passed "
            "JOIN qc_h5_coverage_snapshot AS coverage "
            "ON coverage.coverage_snapshot_id=gate.h5_coverage_snapshot_id "
            "AND coverage.payload_hash=gate.h5_coverage_snapshot_hash "
            "AND coverage.task_id=task.id AND coverage.agent_run_id=task.finalization_agent_run_id "
            "AND coverage.finalization_epoch=gate.h5_coverage_snapshot_epoch "
            "WHERE task.id=$1::uuid AND task.finalization_state='sealed' "
            "AND task.finalization_agent_run_id=$2::uuid "
            "AND task.finalization_epoch=$3::integer+1 AND gate.h5_coverage_snapshot_epoch=$3",
            task_id,
            agent_run_id,
            epoch,
        )
    )
