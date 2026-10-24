"""为绿色页面判定补读同任务、同原件的正式动作身份，不写入或制造动作。"""

import json
from types import SimpleNamespace

from services.api.app.infrastructure.db.green_process_evidence import _valid_action


async def read_green_page_action_context(pool, *, task_id, capture_roles, source_agent_run_id):
    capture_ids = tuple(dict.fromkeys(capture_roles.values()))
    async with (
        pool.acquire() as connection,
        connection.transaction(
            isolation="repeatable_read",
            readonly=True,
        ),
    ):
        platform = await connection.fetchval(
            "SELECT target_platform FROM qc_task WHERE id=$1::uuid AND deleted_at IS NULL",
            task_id,
        )
        captures = await connection.fetch(
            "SELECT capture.*,session.finalization_epoch AS session_epoch FROM qc_capture capture "
            "JOIN qc_surface_session session ON session.session_id=capture.surface_session_id "
            "AND session.task_id=capture.task_id "
            "WHERE capture.task_id=$1::uuid AND capture.id=ANY($2::uuid[]) "
            "AND capture.capture_status='captured' AND capture.agent_run_id=$3::uuid",
            task_id,
            list(capture_ids),
            source_agent_run_id,
        )
        if {str(row["id"]) for row in captures} != set(capture_ids):
            return platform, {}, ("green_page_action_capture_identity_mismatch",)
        by_id = {str(row["id"]): row for row in captures}
        rows = await connection.fetch(
            "SELECT action.* FROM qc_action_execution action JOIN qc_capture origin "
            "ON origin.id=action.requested_source_capture_id AND origin.task_id=action.task_id "
            "AND origin.agent_run_id=$3::uuid "
            "WHERE action.task_id=$1::uuid AND action.source_capture_id=ANY($2::uuid[]) "
            "AND action.result_capture_id=ANY($2::uuid[]) "
            "ORDER BY action.created_at,action.action_execution_id",
            task_id,
            list(capture_ids),
            source_agent_run_id,
        )
        actions, issues = {}, []
        for row in rows:
            pair = []
            for key in ("source_capture_id", "result_capture_id"):
                raw = by_id.get(str(row[key]))
                if raw is None:
                    break
                pair.append(
                    SimpleNamespace(
                        capture_id=str(raw["id"]),
                        session_id=raw["surface_session_id"],
                        role=raw["capture_role"],
                        action_id=raw["action_execution_id"],
                        command_sequence=raw["session_command_sequence"],
                        parent_capture_id=str(raw["parent_capture_id"])
                        if raw["parent_capture_id"]
                        else None,
                        captured_at=raw["captured_at"],
                    )
                )
            if (
                len(pair) != 2
                or not _valid_action(row, *pair)
                or any(
                    by_id[item.capture_id]["session_epoch"] != row["finalization_epoch"]
                    for item in pair
                )
            ):
                issues.append("green_page_formal_action_lineage_mismatch")
                continue
            payload = row["intent_payload"]
            actions[row["action_execution_id"]] = {
                **(json.loads(payload) if isinstance(payload, str) else dict(payload)),
                "_source_capture_id": str(row["source_capture_id"]),
                "_result_capture_id": str(row["result_capture_id"]),
                "_session_id": row["session_id"],
                "_action_type": (json.loads(payload) if isinstance(payload, str) else payload)[
                    "action_type"
                ],
                "_command_sequence": row["command_sequence"],
                "_started_at": row["started_at"].isoformat(),
                "_finished_at": row["action_finished_at"].isoformat(),
            }
        return platform, actions, tuple(dict.fromkeys(issues))
