"""Web 目标占有与复用：数据库事务锁定 Run，禁止跨版本或 Epoch 复用。"""

from __future__ import annotations

import json
from typing import Any

from services.api.app.application.web_run_policy import materialize_web_policy
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    read_web_policy_on_connection,
)


class PostgresWebLogicalTargetRepository:
    def __init__(self, *, pool: Any = None, database_url: str | None = None):
        self._pool = pool
        self._database_url = database_url

    async def claim(self, **kwargs: Any) -> dict[str, Any]:
        return await self._access(create=True, **kwargs)

    async def find(self, **kwargs: Any) -> dict[str, Any] | None:
        return await self._access(create=False, **kwargs)

    async def _access(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        source_capture_id: str,
        url: str,
        create: bool,
    ):
        pool = self._pool or await get_postgres_pool(self._database_url)
        # 只读别名查询使用一致快照，不争抢发布事务持有的 Run/Session 行锁。
        options = {} if create else {"isolation": "repeatable_read", "readonly": True}
        async with pool.acquire() as connection, connection.transaction(**options):
            scope, policy = await _require_scope(
                connection, task_id, agent_run_id, session_id, source_capture_id, lock=create
            )
            logical_id = policy.logical_id(url)
            alias = await connection.fetchval(
                "SELECT logical_page_id FROM qc_web_logical_target_alias WHERE "
                "agent_run_id=$1::uuid AND alias_logical_page_id=$2",
                agent_run_id,
                logical_id,
            )
            logical_id = alias or logical_id
            row = await connection.fetchrow(
                "SELECT * FROM qc_web_logical_target WHERE agent_run_id=$1::uuid AND "
                "logical_page_id=$2",
                agent_run_id,
                logical_id,
            )
            created = row is None and create
            if created:
                await connection.execute(
                    "INSERT INTO "
                    "qc_web_logical_target(agent_run_id,logical_page_id,task_id,session_id,"
                    "finalization_epoch,identity_policy_version,source_capture_id) "
                    "VALUES($1::uuid,$2,$3::uuid,$4,$5,$6,$7::uuid)",
                    agent_run_id,
                    logical_id,
                    task_id,
                    session_id,
                    scope["finalization_epoch"],
                    policy.version,
                    source_capture_id,
                )
                row = await connection.fetchrow(
                    "SELECT * FROM qc_web_logical_target WHERE agent_run_id=$1::uuid AND "
                    "logical_page_id=$2",
                    agent_run_id,
                    logical_id,
                )
            if row is None:
                return None
            result = _target_row(row)
            if (
                result["session_id"] != session_id
                or result["finalization_epoch"] != scope["finalization_epoch"]
                or result["identity_policy_version"] != policy.version
            ):
                raise ValueError("WEB_LOGICAL_TARGET_SCOPE_INVALID")
            if result["assessment_capture_id"]:
                from services.api.app.infrastructure.db.web_capture_identity import (
                    read_web_capture_identities,
                )

                identities = await read_web_capture_identities(
                    connection, task_id=task_id, agent_run_id=agent_run_id
                )
                identity = identities.get(result["assessment_capture_id"], {})
                if (
                    identity.get("logical_page_identity_status") != "verified"
                    or identity.get("logical_page_id") != result["logical_page_id"]
                ):
                    raise ValueError("WEB_LOGICAL_TARGET_MATERIAL_MISSING")
                valid = await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM qc_h5_page_type_assessment a JOIN qc_capture c "
                    "ON c.id=a.page_capture_id "
                    "WHERE a.page_capture_id=$1::uuid AND a.agent_run_id=$2::uuid AND "
                    "a.session_id=$3 "
                    "AND a.schema_version IN ('web-page-type-assessment.v1', 'web-page-type-assessment.v2', 'web-page-type-assessment.v3', 'web-page-type-assessment.v4') AND "
                    "c.capture_status='captured' "
                    "AND c.metadata->>'finalization_epoch'=$4)",
                    result["assessment_capture_id"],
                    agent_run_id,
                    session_id,
                    str(scope["finalization_epoch"]),
                )
                if not valid:
                    raise ValueError("WEB_LOGICAL_TARGET_MATERIAL_MISSING")
            result["created"] = created
            result["reason_code"] = (
                "WEB_LOGICAL_TARGET_INCOMPLETE" if result["state"] == "claimed" else None
            )
            return result

    async def record_capture_redirect_aliases(
        self, *, task_id, agent_run_id, session_id, source_capture_id, url
    ):
        from urllib.parse import urlsplit

        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            scope, policy = await _require_scope(
                connection, task_id, agent_run_id, session_id, source_capture_id
            )
            logical_id = policy.logical_id(url)
            rows = await connection.fetch(
                """
                WITH RECURSIVE lineage AS (
                    SELECT id,parent_capture_id,metadata,action_execution_id,0 AS depth
                    FROM qc_capture WHERE id=$1::uuid AND task_id=$2::uuid
                    UNION ALL
                    SELECT parent.id,parent.parent_capture_id,parent.metadata,
                           parent.action_execution_id,lineage.depth+1
                    FROM qc_capture parent JOIN lineage ON parent.id=lineage.parent_capture_id
                    WHERE lineage.depth<20 AND parent.task_id=$2::uuid AND
                    parent.agent_run_id=$3::uuid
                      AND parent.surface_session_id=$4 AND parent.capture_status='captured'
                      AND parent.metadata->>'finalization_epoch'=$5
                )
                SELECT lineage.id::text AS capture_id,
                       lineage.metadata->'pipeline_metrics'->'web_natural_redirect_chain' AS chain,
                       action.intent_payload #>> '{parameters,destination_url}' AS requested_url
                FROM lineage JOIN qc_action_execution action ON
                action.action_execution_id=lineage.action_execution_id
                JOIN qc_capture before_capture ON before_capture.id=action.source_capture_id
                WHERE action.task_id=$2::uuid AND action.session_id=$4 AND action.status='completed'
                  AND action.finalization_epoch=$5::integer AND action.result_capture_id=lineage.id
                  AND action.source_capture_id=lineage.parent_capture_id
                  AND before_capture.agent_run_id=$3::uuid AND before_capture.surface_session_id=$4
                  AND before_capture.capture_role='before_action' AND
                  before_capture.capture_status='captured'
                  AND before_capture.action_execution_id=action.action_execution_id
                  AND lineage.metadata->'pipeline_metrics'->'web_natural_redirect_chain' IS NOT NULL
                ORDER BY lineage.depth
                """,
                source_capture_id,
                task_id,
                agent_run_id,
                session_id,
                str(scope["finalization_epoch"]),
            )
            for row in rows:
                chain = json.loads(row["chain"]) if isinstance(row["chain"], str) else row["chain"]
                if not isinstance(chain, list) or len(chain) < 2 or not row["requested_url"]:
                    continue
                try:
                    normalized = [policy.canonical(value) for value in chain]
                    if (
                        policy.canonical(row["requested_url"]) != normalized[0]
                        or policy.logical_id(normalized[-1]) != logical_id
                    ):
                        continue
                    origins = [urlsplit(value) for value in normalized]
                    if any(part.hostname != origins[0].hostname for part in origins) or any(
                        a.scheme == "https" and b.scheme == "http"
                        for a, b in zip(origins, origins[1:], strict=False)
                    ):
                        continue
                    alias_id = policy.logical_id(normalized[0])
                except ValueError:
                    continue
                if alias_id == logical_id:
                    continue
                occupied = await connection.fetchval(
                    "SELECT logical_page_id FROM qc_web_logical_target WHERE "
                    "agent_run_id=$1::uuid AND logical_page_id=$2",
                    agent_run_id,
                    alias_id,
                )
                if occupied:
                    raise ValueError("WEB_LOGICAL_ALIAS_CONFLICT")
                await connection.execute(
                    "INSERT INTO "
                    "qc_web_logical_target_alias(agent_run_id,alias_logical_page_id,"
                    "logical_page_id,task_id,evidence_capture_id,evidence_kind) "
                    "VALUES($1::uuid,$2,$3,$4::uuid,$5::uuid,'natural_redirect') ON CONFLICT DO "
                    "NOTHING",
                    agent_run_id,
                    alias_id,
                    logical_id,
                    task_id,
                    row["capture_id"],
                )
                actual = await connection.fetchval(
                    "SELECT logical_page_id FROM qc_web_logical_target_alias WHERE "
                    "agent_run_id=$1::uuid AND alias_logical_page_id=$2",
                    agent_run_id,
                    alias_id,
                )
                if actual != logical_id:
                    raise ValueError("WEB_LOGICAL_ALIAS_CONFLICT")

    async def record_menu_reuse(self, *, command, site_id, entry, source_capture_id, target):
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            scope, policy = await _require_scope(
                connection,
                command.task_id,
                command.agent_run_id,
                command.session_id,
                source_capture_id,
            )
            from services.api.app.application.web_runtime_discovery import (
                WebRuntimeMenuEntry,
                discovery_safety_reason,
            )
            if isinstance(entry, WebRuntimeMenuEntry):
                from services.api.app.application.web_run_policy import materialize_web_policy
                from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
                    read_web_policy_on_connection,
                )
                frozen = materialize_web_policy(await read_web_policy_on_connection(
                    connection, command.task_id, command.agent_run_id))
                if (not frozen.discovery_policy.enabled or entry.candidate.source_capture_id != source_capture_id
                        or discovery_safety_reason(entry.candidate, site_origin=command.target_url)):
                    raise ValueError("WEB_DISCOVERY_REUSE_NOT_AUTHORIZED")
            requested_id = policy.logical_id(entry.canonical_url)
            resolved_id = await connection.fetchval(
                "SELECT logical_page_id FROM qc_web_logical_target_alias WHERE "
                "agent_run_id=$1::uuid AND alias_logical_page_id=$2",
                command.agent_run_id,
                requested_id,
            )
            if (resolved_id or requested_id) != target["logical_page_id"]:
                raise ValueError("WEB_LOGICAL_TARGET_REUSE_CONFLICT")
            from hashlib import sha256

            existing = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM qc_web_menu_ledger_entry entry JOIN "
                "qc_web_logical_target target ON target.agent_run_id=entry.agent_run_id "
                "AND target.logical_page_id=entry.diagnostic->>'reused_logical_page_id' AND "
                "target.assessment_capture_id::text=entry.diagnostic->>'reused_capture_id' "
                "WHERE entry.agent_run_id=$1::uuid AND entry.site_id=$2 AND "
                "entry.menu_entry_id=$3 AND entry.status='logical_target_reused' "
                "AND entry.menu_action_count=0 AND entry.detail_action_count=0 AND "
                "target.logical_page_id=$4 AND target.assessment_capture_id::text=$5 "
                "AND target.finalization_epoch=$6 AND target.identity_policy_version=$7 AND "
                "target.state='materialized' AND entry.canonical_url_sha256=$8)",
                command.agent_run_id,
                site_id,
                entry.entry_id,
                target["logical_page_id"],
                target["assessment_capture_id"],
                scope["finalization_epoch"],
                policy.version,
                sha256(entry.canonical_url.encode("utf-8")).hexdigest(),
            )
            if existing:
                return
            changed = await connection.fetchval(
                "UPDATE qc_web_menu_ledger_entry entry SET status='logical_target_reused', "
                "diagnostic=jsonb_build_object('reused_target_stage','menu','reused_capture_id',$1::text,"
                "'reused_logical_page_id',$2::text), row_version=row_version+1 "
                "WHERE entry.agent_run_id=$4::uuid AND entry.site_id=$5 AND entry.menu_entry_id=$6 "
                "AND entry.canonical_url_sha256=$9 AND EXISTS(SELECT 1 FROM "
                "qc_web_exploration_budget budget WHERE budget.agent_run_id=entry.agent_run_id "
                "AND budget.task_id=$3::uuid AND budget.state='open') "
                "AND (entry.status='pending' OR (entry.source_kind='runtime_discovered' "
                "AND entry.decision='runtime_record_only' AND entry.status='runtime_discovered_unreviewed')) "
                "AND entry.menu_action_count=0 AND "
                "entry.detail_action_count=0 "
                "AND EXISTS(SELECT 1 FROM qc_web_logical_target target WHERE "
                "target.agent_run_id=entry.agent_run_id "
                "AND target.logical_page_id=$2 AND target.assessment_capture_id::text=$1 AND "
                "target.state='materialized' "
                "AND target.finalization_epoch=$7 AND target.identity_policy_version=$8) "
                "RETURNING entry.menu_entry_id",
                target["assessment_capture_id"],
                target["logical_page_id"],
                command.task_id,
                command.agent_run_id,
                site_id,
                entry.entry_id,
                scope["finalization_epoch"],
                policy.version,
                sha256(entry.canonical_url.encode("utf-8")).hexdigest(),
            )
            if changed is None:
                raise ValueError("WEB_LOGICAL_TARGET_REUSE_CONFLICT")

    async def claim_primary(self, *, task_id, agent_run_id, session_id, source_capture_id, url):
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            scope, policy = await _require_scope(
                connection, task_id, agent_run_id, session_id, source_capture_id
            )
            row = await connection.fetchrow(
                "SELECT * FROM qc_web_logical_target WHERE agent_run_id=$1::uuid AND "
                "logical_page_id=$2 AND finalization_epoch=$3",
                agent_run_id,
                policy.logical_id(url),
                scope["finalization_epoch"],
            )
            if row is None or row["state"] != "materialized":
                raise ValueError("WEB_LOGICAL_TARGET_INCOMPLETE")
            created = row["primary_state"] == "pending"
            if created:
                await connection.execute(
                    "UPDATE qc_web_logical_target SET primary_state='claimed' WHERE "
                    "agent_run_id=$1::uuid AND logical_page_id=$2",
                    agent_run_id,
                    policy.logical_id(url),
                )
            return dict(_target_row(row), created=created)

    async def finish_primary(
        self, *, task_id, agent_run_id, session_id, source_capture_id, url, result
    ):
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            # 源 Capture 保持旧主材料引用；新补图另行要求当前证据，禁止改写父链。
            scope, policy = await _require_scope(
                connection,
                task_id,
                agent_run_id,
                session_id,
                source_capture_id,
                require_current=False,
            )
            evidence_id = result.get("evidence_capture_id")
            if evidence_id:
                await _require_scope(connection, task_id, agent_run_id, session_id, evidence_id)
            changed = await connection.fetchval(
                "UPDATE qc_web_logical_target SET "
                "primary_state='complete',primary_capture_id=$1::uuid,primary_result=$2::jsonb "
                "WHERE agent_run_id=$3::uuid AND logical_page_id=$4 AND finalization_epoch=$5 "
                "AND primary_state='claimed' RETURNING logical_page_id",
                evidence_id,
                json.dumps(result, ensure_ascii=False),
                agent_run_id,
                policy.logical_id(url),
                scope["finalization_epoch"],
            )
            if not changed:
                raise ValueError("WEB_LOGICAL_PRIMARY_CLAIM_CONFLICT")

    async def claim_refinement(self, *, task_id, agent_run_id, session_id, source_capture_id, url):
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            scope, policy = await _require_scope(
                connection, task_id, agent_run_id, session_id, source_capture_id
            )
            return bool(
                await connection.fetchval(
                    "UPDATE qc_web_logical_target SET refinement_capture_id=$1::uuid "
                    "WHERE agent_run_id=$2::uuid AND logical_page_id=$3 AND finalization_epoch=$4 "
                    "AND primary_capture_id=$1::uuid AND refinement_capture_id IS NULL AND "
                    "assessment->>'page_type' IS NULL RETURNING logical_page_id",
                    source_capture_id,
                    agent_run_id,
                    policy.logical_id(url),
                    scope["finalization_epoch"],
                )
            )

    async def materialize(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        capture_id: str,
        url: str,
        assessment: dict[str, Any],
    ):
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            scope, policy = await _require_scope(
                connection, task_id, agent_run_id, session_id, capture_id
            )
            record = await connection.fetchval(
                "SELECT assessment_record_id FROM qc_h5_page_type_assessment WHERE "
                "task_id=$1::uuid AND agent_run_id=$2::uuid "
                "AND session_id=$3 AND page_capture_id=$4::uuid AND "
                "schema_version IN ('web-page-type-assessment.v1', 'web-page-type-assessment.v2', 'web-page-type-assessment.v3', 'web-page-type-assessment.v4')",
                task_id,
                agent_run_id,
                session_id,
                capture_id,
            )
            if not record:
                raise ValueError("WEB_LOGICAL_TARGET_MATERIAL_MISSING")
            changed = await connection.fetchval(
                "UPDATE qc_web_logical_target SET "
                "state='materialized',assessment_capture_id=$1::uuid,assessment=$2::jsonb "
                "WHERE task_id=$3::uuid AND agent_run_id=$4::uuid AND session_id=$5 AND "
                "finalization_epoch=$6 "
                "AND logical_page_id=$7 AND (state='claimed' OR (refinement_capture_id=$1::uuid "
                "AND assessment_capture_id<>$1::uuid)) RETURNING logical_page_id",
                capture_id,
                json.dumps(assessment, ensure_ascii=False),
                task_id,
                agent_run_id,
                session_id,
                scope["finalization_epoch"],
                policy.logical_id(url),
            )
            if not changed:
                raise ValueError("WEB_LOGICAL_TARGET_CLAIM_CONFLICT")


async def _require_scope(
    connection, task_id, agent_run_id, session_id, capture_id, *, require_current=True, lock=True
):
    scope = await connection.fetchrow(
        """
        SELECT task.finalization_epoch
        FROM qc_task task
        JOIN qc_agent_run run ON run.task_id=task.id AND run.id=$2::uuid
        JOIN qc_surface_session session ON session.task_id=task.id AND session.session_id=$3
        JOIN qc_capture capture ON capture.task_id=task.id AND capture.id=$4::uuid
        WHERE task.id=$1::uuid AND task.target_platform='web'
          AND task.task_config->>'workflow_version'='v2'
          AND task.status='running' AND task.finalization_state='open'
          AND run.agent_type='main' AND run.status='running'
          AND run.id=(SELECT latest.id FROM qc_agent_run latest WHERE latest.task_id=task.id AND
          latest.agent_type='main' ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
          AND session.session_id='web:' || run.id::text AND session.session_owner='web-v2-workflow'
          AND session.surface_kind='web' AND session.status='active'
          AND session.finalization_epoch=task.finalization_epoch
          AND capture.agent_run_id=run.id AND capture.surface_session_id=session.session_id
          AND capture.surface_kind='web' AND capture.capture_status='captured'
          AND capture.metadata->>'finalization_epoch'=task.finalization_epoch::text
          AND (NOT $5::boolean OR session.current_capture_id=capture.id)
        """ + (" FOR UPDATE OF run,session" if lock else ""),
        task_id,
        agent_run_id,
        session_id,
        capture_id,
        require_current,
    )
    if scope is None:
        raise ValueError("WEB_LOGICAL_TARGET_SCOPE_INVALID")
    snapshot = await read_web_policy_on_connection(connection, task_id, agent_run_id)
    if snapshot is None:
        raise ValueError("WEB_POLICY_HISTORY_MISSING")
    policy = materialize_web_policy(snapshot)
    return scope, policy.identity_policy


def _target_row(row):
    result = dict(row)
    for name in ("assessment", "primary_result"):
        if isinstance(result[name], str):
            result[name] = json.loads(result[name])
    for name in (
        "task_id",
        "agent_run_id",
        "source_capture_id",
        "assessment_capture_id",
        "primary_capture_id",
    ):
        if result[name] is not None:
            result[name] = str(result[name])
    return result
