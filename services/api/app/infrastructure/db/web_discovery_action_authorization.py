"""目录外动作只消费冻结 Run、已领取台账和 Broker 实际动作前 Capture。"""

import json

from services.api.app.application.web_run_policy import materialize_web_policy
from services.api.app.domain.interaction.validation import thaw_mapping
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    read_web_policy_on_connection,
)


class PostgresWebDiscoveryActionAuthorization:
    def __init__(self, *, snapshot, pool=None):
        self._snapshot = snapshot
        self._pool = pool

    async def authorize(self, request):
        frozen = self._snapshot
        if request.task_id != frozen.task_id or request.session_id != "web:" + frozen.agent_run_id:
            return False
        if not materialize_web_policy(frozen).discovery_policy.enabled:
            return False
        parameters = request.parameters
        source = parameters.get("web_discovery_source_capture_id")
        candidate = parameters.get("web_discovery_candidate")
        if not isinstance(source, str) or not candidate:
            return False
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            actual = await read_web_policy_on_connection(
                connection, frozen.task_id, frozen.agent_run_id
            )
            if actual != frozen:
                return False
            authorization = await connection.fetchval(
                """
                SELECT entry.discovery_manifest->'authorization'
                FROM qc_web_menu_ledger_entry entry
                JOIN qc_web_exploration_budget budget ON budget.agent_run_id=entry.agent_run_id
                  AND budget.site_id=entry.site_id AND budget.state='open'
                JOIN qc_task task ON task.id=budget.task_id AND task.target_platform='web'
                  AND task.task_config->>'workflow_version'='v2'
                  AND task.status='running' AND task.finalization_state='open'
                JOIN qc_agent_run run ON run.id=budget.agent_run_id AND run.task_id=task.id
                  AND run.agent_type='main' AND run.status='running'
                  AND run.id=(SELECT latest.id FROM qc_agent_run latest
                    WHERE latest.task_id=task.id AND latest.agent_type='main'
                    ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
                JOIN qc_surface_session session ON session.task_id=task.id
                  AND session.session_id=budget.session_id
                  AND session.session_owner='web-v2-workflow' AND session.status='active'
                  AND session.finalization_epoch=task.finalization_epoch
                JOIN qc_action_execution action ON action.task_id=task.id
                  AND action.session_id=session.session_id AND action.intent_id=entry.menu_intent_id
                  AND action.status='executing'
                  AND action.requested_source_capture_id=entry.menu_source_capture_id
                  AND action.finalization_epoch=task.finalization_epoch
                JOIN qc_capture before ON before.id=action.source_capture_id AND before.id=$4::uuid
                  AND before.task_id=task.id AND before.agent_run_id=run.id
                  AND before.surface_session_id=session.session_id
                  AND before.capture_role='before_action'
                  AND before.action_execution_id=action.action_execution_id
                  AND before.session_command_sequence=action.command_sequence
                  AND before.parent_capture_id=entry.menu_source_capture_id
                  AND before.metadata->>'finalization_epoch'=task.finalization_epoch::text
                WHERE task.id=$1::uuid AND run.id=$2::uuid AND session.session_id=$3
                  AND entry.source_kind='runtime_discovered'
                  AND entry.decision='runtime_observe_only'
                  AND entry.status='menu_reserved' AND entry.menu_action_count=1
                  AND entry.menu_source_capture_id::text=$5
                  AND entry.menu_entry_id=$6 AND entry.execution_slot IS NOT NULL
                  AND session.current_capture_id IN (entry.menu_source_capture_id,before.id)
            """,
                frozen.task_id,
                frozen.agent_run_id,
                request.session_id,
                request.source_capture_id,
                source,
                parameters.get("web_menu_entry_id"),
            )
        if authorization is None:
            return False
        authorization = (
            json.loads(authorization) if isinstance(authorization, str) else authorization
        )
        # ActionIntent 将所有嵌套数组冻结为 tuple；与 PG JSON 比较前统一还原。
        declared = thaw_mapping(candidate)
        return (
            authorization.get("policy_sha256") == frozen.payload_sha256
            and authorization.get("source_capture_id") == source
            and authorization.get("candidate") == declared
        )
