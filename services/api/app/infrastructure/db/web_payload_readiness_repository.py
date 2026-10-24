"""Web 阶段需求/Manifest 原子收敛；与真实 Gate 共用连接和既有锁。"""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime

from services.api.app.application.evidence_exit_gate import (
    EvidenceExitGateBlockedError,
    requires_evidence_exit_gate,
)
from services.api.app.application.evidence_exit_requirements import EvidenceExitRequirementResolver
from services.api.app.application.web_payload_manifest import adjudicate_web_payload_manifest
from services.api.app.domain.evidence import EvidenceQualityStatus
from services.api.app.domain.exploration.evidence_exit_requirements import (
    EvidenceExitRequirementSet,
    EvidenceExitRequirementStatus,
)
from services.api.app.domain.exploration.web_payload_requirements import (
    WebPayloadRequirementSet,
    create_payload_requirement_set,
    rebuild_payload_requirement_set,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.capture_epoch import capture_epoch_sql
from services.api.app.infrastructure.db.evidence_exit_gate_repository import (
    PostgresEvidenceExitGate,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    PostgresWebRunPolicySnapshotRepository,
)


class _ConnectionPool:
    """借用当前事务连接；绝不获取第二连接或关闭调用者连接。"""

    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class PostgresWebPayloadReadinessRepository:
    def __init__(self, *, artifact_store, pool=None, reservation_seconds=120):
        self._artifact_store = artifact_store
        self._pool = pool
        self._reservation_seconds = reservation_seconds

    async def _get_pool(self):
        return self._pool if self._pool is not None else await get_postgres_pool()

    async def preserve_and_reserve(self, *, intent, permit):
        if not requires_evidence_exit_gate(intent):
            return None
        pool = await self._get_pool()
        blocked = None
        async with asyncio.timeout(30), pool.acquire() as connection, connection.transaction():
            context = await self._lock_context(
                connection,
                task_id=intent.task_id,
                session_id=intent.session_id,
                capture_id=intent.source_capture_id,
                permit=permit,
            )
            await self._converge(connection, context=context, classified=False)
            # 对象 I/O 后重新验证租约时间及当前身份；Gate 使用同连接，避免锁自锁。
            await self._lock_context(
                connection,
                task_id=intent.task_id,
                session_id=intent.session_id,
                capture_id=intent.source_capture_id,
                permit=permit,
            )
            try:
                decision = await PostgresEvidenceExitGate(
                    pool=_ConnectionPool(connection), reservation_seconds=self._reservation_seconds
                ).reserve(intent=intent, permit=permit)
            except EvidenceExitGateBlockedError as error:
                blocked = error
        if blocked is not None:
            raise blocked
        return decision

    async def finalize_classified_capture(self, *, task_id, agent_run_id, session_id, capture_id):
        pool = await self._get_pool()
        async with asyncio.timeout(30), pool.acquire() as connection, connection.transaction():
            context = await self._lock_context(
                connection,
                task_id=task_id,
                session_id=session_id,
                capture_id=capture_id,
                agent_run_id=agent_run_id,
            )
            if context["page_type"] not in {
                "content_list",
                "content_detail",
                "video_list",
                "video_detail",
            }:
                return None
            return await self._converge(connection, context=context, classified=True)

    async def _lock_context(
        self, connection, *, task_id, session_id, capture_id, permit=None, agent_run_id=None
    ):
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{task_id}"
        )
        await connection.execute("SELECT 1 FROM qc_task WHERE id=$1::uuid FOR UPDATE", task_id)
        await connection.execute(
            "SELECT 1 FROM qc_surface_session WHERE task_id=$1::uuid AND session_id=$2 FOR UPDATE",
            task_id,
            session_id,
        )
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))", f"evidence-exit:{task_id}:{capture_id}"
        )
        row = await connection.fetchrow(_CONTEXT_SQL, task_id, session_id, capture_id)
        if row is None or (agent_run_id is not None and row["agent_run_id"] != agent_run_id):
            raise ValueError("WEB_PAYLOAD_CONTEXT_REJECTED")
        if permit is not None and (
            permit.task_id != task_id
            or permit.session_id != session_id
            or permit.surface_kind.value != "web"
            or row["current_capture_id"] != capture_id
            or row["lease_owner"] != permit.lease_owner
            or row["lease_token"] != permit.lease_token
            or not row["lease_valid"]
            or row["command_sequence"] != permit.command_sequence
            or row["finalization_epoch"] != permit.finalization_epoch
        ):
            raise ValueError("WEB_PAYLOAD_LEASE_REJECTED")
        return dict(row)

    async def _converge(self, connection, *, context, classified):
        borrowed = _ConnectionPool(connection)
        task, run, session, capture = (
            context[key] for key in ("task_id", "agent_run_id", "session_id", "capture_id")
        )
        policy = await PostgresWebRunPolicySnapshotRepository(pool=borrowed).read(
            task_id=task, agent_run_id=run
        )
        if policy is None:
            raise ValueError("WEB_PAYLOAD_FROZEN_POLICY_REQUIRED")
        manifests = PostgresEvidenceManifestRepository(pool=borrowed)
        manifest = await manifests.get_latest_for_capture(task_id=task, capture_id=capture)
        if manifest is None:
            raise ValueError("WEB_PAYLOAD_MANIFEST_MISSING")
        payload = await connection.fetchval(
            "SELECT requirement_payload FROM qc_evidence_exit_requirement_set "
            "WHERE capture_id=$1::uuid ORDER BY revision DESC LIMIT 1",
            capture,
        )
        requirements = (
            EvidenceExitRequirementSet.from_dict(
                json.loads(payload) if isinstance(payload, str) else payload
            )
            if payload
            else None
        )
        if requirements is not None and not isinstance(requirements, WebPayloadRequirementSet):
            # 不把历史业务集合重标新协议；真实旧捕获保持原控制面，不回填历史。
            raise ValueError("WEB_PAYLOAD_LEGACY_SET_UNSUPPORTED")
        now = datetime.now(UTC)
        fresh = create_payload_requirement_set(
            task_id=task,
            agent_run_id=run,
            session_id=session,
            finalization_epoch=context["finalization_epoch"],
            capture_id=capture,
            policy_hash=policy.payload_sha256,
            capture_role=context["capture_role"],
            committed_artifact_types=[item.artifact_type for item in manifest.artifacts],
            created_at=now,
        )
        if requirements is None:
            requirements = fresh
            await self._append(connection, previous=None, current=requirements)
        elif requirements.binding != fresh.binding:
            raise ValueError("WEB_PAYLOAD_BINDING_DRIFT")
        phase = "capture_preservation"
        if classified:
            candidates = await PostgresTaskRepository(pool=borrowed).list_candidate_rules(
                task_id=task, page_type=context["page_type"], auth_gate=None
            )
            if len(candidates) > 1000:
                raise ValueError("WEB_PAYLOAD_RULE_BOUND_EXCEEDED")
            resolved = EvidenceExitRequirementResolver().resolve(
                task_id=task,
                session_id=session,
                capture_id=capture,
                candidate_rules=candidates,
                capture_role=context["capture_role"],
                created_at=now,
            )
            known = {item.requirement_id: item for item in requirements.requirements}
            for item in resolved.requirements:
                old = known.get(item.requirement_id)
                if old is not None and (
                    old.required_artifact_types != item.required_artifact_types
                    or old.required_capture_roles != item.required_capture_roles
                    or old.min_screenshot_count != item.min_screenshot_count
                    or old.allow_empty_visible_text != item.allow_empty_visible_text
                ):
                    raise ValueError("WEB_PAYLOAD_BUSINESS_CONTRACT_DRIFT")
            members = (
                *requirements.requirements,
                *(item for item in resolved.requirements if item.requirement_id not in known),
            )
            matcher = stable_protocol_hash(
                {
                    "phase": "classified_rule_requirements",
                    "binding": dict(requirements.binding),
                    "candidate_matcher": resolved.matcher_snapshot_hash,
                }
            )
            if (
                members != requirements.requirements
                or matcher != requirements.matcher_snapshot_hash
            ):
                newer = rebuild_payload_requirement_set(
                    requirements,
                    requirements=members,
                    matcher_snapshot_hash=matcher,
                    created_at=now,
                )
                await self._append(connection, previous=requirements, current=newer)
                requirements = newer
            phase = "classified_rule_requirements"
        elif len(requirements.requirements) > 1:
            phase = "classified_rule_requirements"
        result = await adjudicate_web_payload_manifest(
            manifest=manifest,
            requirements=requirements,
            artifact_store=self._artifact_store,
            phase=phase,
            now=now,
        )
        # 相同完整需求且当前对象重新验证通过时可复用已有版本；新成员不得走此分支。
        stage = (
            manifest.artifacts[0].metadata.get("web_readiness_stage", {})
            if manifest.artifacts
            else {}
        )
        if (
            result.quality_status is EvidenceQualityStatus.PASS
            and manifest.quality_status is EvidenceQualityStatus.PASS
            and requirements.nonterminal_count == 0
            and stage.get("matcher_snapshot_hash") == requirements.matcher_snapshot_hash
            and stage.get("binding") == dict(requirements.binding)
            and set(stage.get("adjudicated_requirement_ids", ()))
            == {item.requirement_id for item in requirements.requirements}
        ):
            return manifest
        await manifests.add(manifest=result)
        if result.quality_status is EvidenceQualityStatus.PASS:
            members = tuple(
                replace(
                    item,
                    status=EvidenceExitRequirementStatus.SATISFIED,
                    resolution_manifest_id=result.manifest_id,
                )
                if item.status is EvidenceExitRequirementStatus.PENDING
                else item
                for item in requirements.requirements
            )
            if members != requirements.requirements:
                newer = rebuild_payload_requirement_set(
                    requirements, requirements=members, created_at=now
                )
                await self._append(connection, previous=requirements, current=newer)
        return result

    async def _append(self, connection, *, previous, current):
        if previous is not None:
            known = {item.requirement_id: item for item in current.requirements}
            for old in previous.requirements:
                new = known.get(old.requirement_id)
                if new is None or (old.status.is_terminal and new != old):
                    raise ValueError("WEB_PAYLOAD_REQUIREMENT_APPEND_INVALID")
        # 仅同一个已验证事务内调用；成员满足必须绑定含该成员裁决证明的 PASS 版本。
        for item in current.requirements:
            if item.status is not EvidenceExitRequirementStatus.SATISFIED:
                continue
            payload = await connection.fetchval(
                "SELECT manifest_payload FROM qc_evidence_manifest WHERE manifest_id=$1 "
                "AND task_id=$2::uuid AND capture_id=$3::uuid AND quality_status='pass'",
                item.resolution_manifest_id,
                current.task_id,
                current.capture_id,
            )
            body = json.loads(payload) if isinstance(payload, str) else payload
            stage = (
                body["artifacts"][0].get("metadata", {}).get("web_readiness_stage", {})
                if body and body.get("artifacts")
                else {}
            )
            if stage.get("binding") != dict(
                current.binding
            ) or item.requirement_id not in stage.get("adjudicated_requirement_ids", []):
                raise ValueError("WEB_PAYLOAD_RESOLUTION_NOT_ADJUDICATED")
        await connection.execute(
            "INSERT INTO qc_evidence_exit_requirement_set "
            "(requirement_set_id,revision,task_id,session_id,capture_id,matcher_snapshot_hash,protocol_version,payload_hash,"
            "requirement_count,nonterminal_count,requirement_payload,created_at) VALUES($1,$2,$3::uuid,$4,$5::uuid,$6,$7,$8,$9,$10,$11::jsonb,$12)",
            current.requirement_set_id,
            current.revision,
            current.task_id,
            current.session_id,
            current.capture_id,
            current.matcher_snapshot_hash,
            current.protocol_version,
            stable_protocol_hash(current.to_dict()),
            len(current.requirements),
            current.nonterminal_count,
            json.dumps(current.to_dict()),
            current.created_at,
        )


_CONTEXT_SQL = f"""
SELECT task.id::text AS task_id,run.id::text AS agent_run_id,session.session_id,capture.id::text AS capture_id,
       capture.capture_role,page.page_type,session.finalization_epoch,session.current_capture_id::text,
       session.lease_owner,session.lease_token::text,session.lease_expires_at>clock_timestamp() AS lease_valid,session.command_sequence
FROM qc_task task JOIN qc_capture capture ON capture.task_id=task.id AND capture.id=$3::uuid
JOIN qc_page_capture page ON page.id=capture.id AND page.task_id=task.id
JOIN qc_agent_run run ON run.id=capture.agent_run_id AND run.task_id=task.id AND run.agent_type='main' AND run.status='running'
 AND COALESCE(run.output_context->>'cancel_requested','false')='false'
JOIN qc_surface_session session ON session.task_id=task.id AND session.session_id=$2
 AND session.session_id=capture.surface_session_id AND session.session_id='web:'||run.id::text
 AND session.surface_kind='web' AND session.session_owner='web-v2-workflow' AND session.status='active'
 AND session.finalization_epoch=task.finalization_epoch
JOIN qc_action_execution action ON action.action_execution_id=capture.action_execution_id AND action.task_id=task.id
 AND action.session_id=session.session_id AND action.finalization_epoch=session.finalization_epoch
WHERE task.id=$1::uuid AND task.target_platform='web' AND task.status='running' AND task.finalization_state='open'
 AND task.task_config->>'workflow_version'='v2'
 AND (task.finalization_recovery_lease_token IS NULL OR task.finalization_recovery_lease_expires_at<=clock_timestamp())
 AND capture.surface_kind='web' AND capture.capture_status='captured'
 AND {capture_epoch_sql("capture")}=session.finalization_epoch
 AND ((capture.capture_role='before_action' AND action.source_capture_id=capture.id)
      OR (capture.capture_role='after_action' AND action.result_capture_id=capture.id))
 AND run.id=(SELECT latest.id FROM qc_agent_run latest WHERE latest.task_id=task.id AND latest.agent_type='main'
             ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
"""
