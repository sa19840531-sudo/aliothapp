"""物理停止事实独立于逻辑lost，按原会话、周期、命令及Run归属核验。"""

import json
from datetime import datetime
from uuid import UUID


def unconfirmed_h5_stop_sql(session="session"):
    return f"""(
        {session}.status IN ('lost','failed')
        AND EXISTS (SELECT 1 FROM qc_task stop_task
                    WHERE stop_task.id={session}.task_id AND stop_task.target_platform='h5'
                      AND stop_task.finalization_epoch={session}.finalization_epoch)
        AND NOT EXISTS (
            SELECT 1 FROM qc_h5_browser_stop_receipt stop_ack
            JOIN qc_task stop_owner ON stop_owner.id=stop_ack.task_id
            WHERE stop_ack.session_id={session}.session_id
              AND stop_ack.task_id={session}.task_id
              AND stop_ack.finalization_epoch={session}.finalization_epoch
              AND stop_ack.command_sequence={session}.command_sequence
              AND stop_ack.owner_agent_run_id=stop_owner.finalization_agent_run_id
        )
    )"""


def validate_browser_stop_proof(session_id, proof):
    if not isinstance(proof, dict):
        raise ValueError("H5停止回执缺少原浏览器证明")
    if (
        proof.get("session_id") != session_id
        or proof.get("method") != "owned_browser_close_ack_and_pid_absent"
        or type(proof.get("browser_pid")) is not int
        or proof["browser_pid"] <= 0
        or proof.get("driver_stop_ack") is not True
    ):
        raise ValueError("H5停止回执与原会话或完整停止合同不一致")
    try:
        UUID(proof["resource_id"])
        observed = datetime.fromisoformat(proof["observed_at"])
        if observed.utcoffset() is None:
            raise ValueError("停止观测缺少时区")
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        raise ValueError("H5停止证明缺少真实资源身份或带时区观测时间") from error


async def record_browser_stop(pool, *, session_id, proof):
    validate_browser_stop_proof(session_id, proof)
    async with pool.acquire() as connection, connection.transaction():
        scope = await connection.fetchrow(
            "SELECT session.task_id,session.finalization_epoch,session.command_sequence,"
            "run.id AS owner FROM qc_surface_session session "
            "JOIN qc_task task ON task.id=session.task_id "
            "JOIN qc_agent_run run ON run.task_id=task.id AND run.agent_type='main' "
            "AND (session.session_id='h5:'||run.id::text "
            "OR session.session_id LIKE 'h5:'||run.id::text||':%') WHERE session.session_id=$1 "
            "AND task.target_platform='h5' AND task.finalization_epoch=session.finalization_epoch "
            "AND (task.finalization_agent_run_id IS NULL OR task.finalization_agent_run_id=run.id) "
            "AND session.status IN ('closing','closed','lost','failed') "
            "FOR UPDATE OF task,session",
            session_id,
        )
        if scope is None:
            raise RuntimeError("H5停止回执的原会话、周期或任务归属不可核验")
        await connection.execute(
            "INSERT INTO qc_h5_browser_stop_receipt(session_id,task_id,owner_agent_run_id,"
            "finalization_epoch,command_sequence,resource_id,proof) "
            "VALUES($1,$2,$3,$4,$5,$6::uuid,$7::jsonb) ON CONFLICT(session_id) DO NOTHING",
            session_id,
            scope["task_id"],
            scope["owner"],
            scope["finalization_epoch"],
            scope["command_sequence"],
            proof["resource_id"],
            json.dumps(proof),
        )
        same = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM qc_h5_browser_stop_receipt WHERE session_id=$1 "
            "AND task_id=$2 AND owner_agent_run_id=$3 AND finalization_epoch=$4 "
            "AND command_sequence=$5 AND proof=$6::jsonb)",
            session_id,
            scope["task_id"],
            scope["owner"],
            scope["finalization_epoch"],
            scope["command_sequence"],
            json.dumps(proof),
        )
        if not same:
            raise RuntimeError("H5原停止回执不可覆写")


async def read_browser_stop(pool, *, session_id):
    async with pool.acquire() as connection:
        value = await connection.fetchval(
            "SELECT ack.proof FROM qc_h5_browser_stop_receipt ack "
            "JOIN qc_surface_session session ON session.session_id=ack.session_id "
            "AND session.task_id=ack.task_id AND session.finalization_epoch=ack.finalization_epoch "
            "AND session.command_sequence=ack.command_sequence "
            "JOIN qc_task task ON task.id=ack.task_id "
            "AND task.finalization_epoch=ack.finalization_epoch "
            "AND (task.finalization_agent_run_id IS NULL "
            "OR task.finalization_agent_run_id=ack.owner_agent_run_id) WHERE ack.session_id=$1",
            session_id,
        )
    return json.loads(value) if isinstance(value, str) else value
