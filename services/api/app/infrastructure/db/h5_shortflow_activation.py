"""提交时冻结，主 Run 启动时原子接线，不改变当前任务与冻结截止。"""

import json
from contextlib import asynccontextmanager

from services.api.app.application.h5_shortflows import H5ShortflowError, freeze_h5_shortflows
from services.api.app.application.journey_definitions import JourneyDefinitionService
from services.api.app.domain.journey.validation import stable_hash
from services.api.app.infrastructure.db.journey_definition_repository import (
    PostgresJourneyDefinitionRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class _ConnectionPool:
    """复用调用方事务连接，让全部流程计划同时提交或回滚。"""

    def __init__(self, connection):
        self._connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self._connection


async def freeze_h5_shortflows_on_connection(connection, *, task_config):
    return await freeze_h5_shortflows(
        task_config=task_config,
        repository=PostgresJourneyDefinitionRepository(pool=_ConnectionPool(connection)),
    )


async def activate_h5_shortflows_for_run(*, task_id, agent_run_id, task_config, pool=None):
    if not task_config.get("h5_shortflows"):
        return ()
    pool = pool or await get_postgres_pool()
    async with pool.acquire() as connection, connection.transaction():
        # 独立等锁后再读取最新主Run，避免单语句沿用等待前的事务快照。
        await connection.execute(
            "SELECT id FROM qc_task WHERE id=$1::uuid FOR UPDATE",
            task_id,
        )
        task = await connection.fetchrow(
            """SELECT task.task_config FROM qc_task AS task
               JOIN qc_agent_run AS run ON run.task_id=task.id
               WHERE task.id=$1::uuid AND run.id=$2::uuid
                 AND task.target_platform='h5' AND task.status='running'
                 AND run.agent_type='main' AND run.status='running'
                 AND task.finalization_state='open'
                 AND (task.finalization_agent_run_id IS NULL
                      OR task.finalization_agent_run_id=run.id)
                 AND run.id=(SELECT latest.id FROM qc_agent_run latest
                     WHERE latest.task_id=task.id AND latest.agent_type='main'
                     ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)""",
            task_id,
            agent_run_id,
        )
        if task is None:
            raise H5ShortflowError("短流程 Task/主 Run/收口身份不匹配")
        authoritative = task["task_config"]
        if isinstance(authoritative, str):
            authoritative = json.loads(authoritative)
        for key in (
            "h5_shortflows",
            "rule_inventory",
            "ruleset_rule_total",
            "execution_mode",
            "workflow_version",
        ):
            if stable_hash(authoritative.get(key)) != stable_hash(task_config.get(key)):
                raise H5ShortflowError("短流程队列配置与任务冻结合同不一致")
        frozen = authoritative["h5_shortflows"]
        selection_keys = {
            "definition_id",
            "version",
            "content_hash",
            "scenario_key",
            "rule_ids",
            "workflow_skill",
        }
        selections = [
            {key: value for key, value in item.items() if key in selection_keys} for item in frozen
        ]
        repository = PostgresJourneyDefinitionRepository(pool=_ConnectionPool(connection))
        checked = await freeze_h5_shortflows(
            task_config={**authoritative, "h5_shortflows": selections},
            repository=repository,
        )
        if checked["h5_shortflows"] != frozen:
            raise H5ShortflowError("短流程发布版本、规则或 Skill 内容漂移")
        service = JourneyDefinitionService(repository=repository)
        plans = []
        for item in frozen:
            plans.append(
                await service.create_run_execution_plan(
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    definition_id=item["definition_id"],
                    version=item["version"],
                    expected_content_hash=item["content_hash"],
                    scenario_key=item["scenario_key"],
                    parameters={
                        "workflow_skill": item["workflow_skill"],
                        "workflow_skill_hash": item["workflow_skill_hash"],
                    },
                    rule_contract_hashes=item["rule_contract_hashes"],
                )
            )
        return tuple(plans)
