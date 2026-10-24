"""Web 多用途合同不兼容的原子技术收口；不放宽通用成员状态 API。"""

from services.api.app.application.rule_execution import RuleExecutionLeaseConflictError
from services.api.app.application.web_sequence_errors import WebRuleContractIncompatibleError
from services.api.app.infrastructure.db.web_sequence_result_projection import (
    lock_web_projection_scope,
    project_web_canonical,
    sync_web_projection,
)


async def terminate_contract_incompatibility(connection, command):
    from services.api.app.infrastructure.db.task_repository import _assert_inspection_write_lease

    scope = await lock_web_projection_scope(connection, command)
    if scope is None:
        raise RuleExecutionLeaseConflictError("技术收口缺少真实 Web 序列参与来源")
    await _assert_inspection_write_lease(
        connection,
        task_id=command.task_id,
        work_item_id=command.work_item_id,
        attempt_id=command.attempt_id,
        lease_token=command.lease_token,
        agent_run_id=command.agent_run_id,
    )
    exact_attempt = await connection.fetchval(
        "SELECT a.id FROM qc_inspection_attempt a JOIN qc_inspection_work_item w ON w.id=a.work_item_id "
        "WHERE a.id=$1::uuid AND a.status='judged' AND a.consumer_agent_run_id=$2::uuid "
        "AND a.consumer_agent_run_id=w.consumer_agent_run_id AND a.attempt_no=w.attempt_count",
        command.attempt_id,
        command.agent_run_id,
    )
    if exact_attempt is None:
        raise RuleExecutionLeaseConflictError("技术收口不是当前已持久原判定 Attempt")
    try:
        # 同事务重新认证原判定、Packet、所有历史来源和实际完整合同差异。
        await project_web_canonical(connection, command, scope)
    except WebRuleContractIncompatibleError:
        pass
    else:
        raise RuleExecutionLeaseConflictError("没有可重证的合同不兼容，不允许技术终结")
    # 批量 Writer 已逐条提交，但正常 member sync 尚未执行；先认证当前真实已提交来源。
    await sync_web_projection(
        connection,
        work_item_id=command.work_item_id,
        lease_token=command.lease_token,
    )
    code = "WEB_RULE_CONTRACT_INCOMPATIBLE"
    message = "原始判定已保留，不同冻结规则标准不能合并；技术收口不代表产品失败"
    await connection.execute(
        "UPDATE qc_inspection_work_item_rule SET status='error',last_error_code=$2,last_error_message=$3,updated_at=now() "
        "WHERE work_item_id=$1::uuid AND status IN ('pending','judged')",
        command.work_item_id,
        code,
        message,
    )
    await connection.execute(
        "UPDATE qc_inspection_attempt SET status='terminal_error',error_code=$2,error_message=$3,finished_at=now(),updated_at=now() "
        "WHERE id=$1::uuid",
        command.attempt_id,
        code,
        message,
    )
    return await connection.fetchval(
        "UPDATE qc_inspection_work_item SET status='failed',last_error_code=$2,last_error_message=$3,"
        "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,finished_at=now(),updated_at=now(),row_version=row_version+1 "
        "WHERE id=$1::uuid RETURNING row_version",
        command.work_item_id,
        code,
        message,
    )
