"""只从真实 Web Capture 的 PASS Manifest 原文形成局部能力修订。"""

import json
from contextlib import asynccontextmanager
from uuid import NAMESPACE_URL, uuid5

from services.api.app.application.manifest_backed_packet import ManifestBackedPacketLoader
from services.api.app.application.web_local_structure import assess_web_local_structure
from services.api.app.application.web_readiness_contracts import read_web_readiness_assessment
from services.api.app.application.web_run_policy import materialize_web_policy
from services.api.app.application.web_sequence_capture_integrity import (
    web_sequence_capture_integrity,
)
from services.api.app.domain.evidence.presentation import PresentationAssessment
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    read_web_policy_on_connection,
)


def _object(value):
    return json.loads(value) if isinstance(value, str) else value


class _RevisionTransactionConnection:
    """只给当前修订清单读取复用持有的事务，不再领取第二条连接。"""

    def __init__(self, connection):
        self._connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self._connection


class PostgresWebCapabilityRevisionRepository:
    def __init__(self, *, artifact_reader, pool=None, database_url=None):
        self._artifacts = artifact_reader
        self._pool = pool
        self._database_url = database_url

    async def record_from_capture(self, *, task_id, agent_run_id, session_id, capture_id):
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{task_id}"
            )
            row = await connection.fetchrow(
                """
                SELECT task.finalization_epoch,capture.captured_at,capture.metadata,capture.action_execution_id,page.url
                FROM qc_task task JOIN qc_agent_run run ON run.task_id=task.id AND run.id=$2::uuid
                JOIN qc_surface_session session ON session.task_id=task.id AND session.session_id=$3
                JOIN qc_capture capture ON capture.task_id=task.id AND capture.id=$4::uuid AND capture.agent_run_id=run.id
                    AND capture.surface_session_id=session.session_id
                JOIN qc_page_capture page ON page.id=capture.id AND page.task_id=task.id
                WHERE task.id=$1::uuid AND task.target_platform='web' AND task.task_config->>'workflow_version'='v2'
                    AND task.task_config->>'execution_mode'='full_inspection'
                    AND task.task_config->>'web_inspection_notification_mode'='enforced' AND task.status='running'
                    AND task.finalization_state IN ('open','exploration_draining','session_draining')
                    AND run.agent_type='main' AND run.status IN ('running','done','failed','aborted')
                    AND COALESCE(run.output_context->>'cancel_requested','false')='false'
                    AND run.id=(SELECT latest.id FROM qc_agent_run latest WHERE latest.task_id=task.id
                        AND latest.agent_type='main' ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
                    AND session.session_id='web:'||run.id::text AND session.session_owner='web-v2-workflow'
                    AND session.surface_kind='web' AND session.status IN ('active','draining')
                    AND session.finalization_epoch=task.finalization_epoch AND capture.capture_role='after_action'
                    AND capture.capture_status='captured' AND capture.metadata->>'finalization_epoch'=task.finalization_epoch::text
                """,
                task_id,
                agent_run_id,
                session_id,
                capture_id,
            )
            if row is None:
                raise ValueError("WEB_CAPABILITY_REVISION_SCOPE_REJECTED")
            frozen = await read_web_policy_on_connection(connection, task_id, agent_run_id)
            if frozen is None:
                raise ValueError("WEB_CAPABILITY_POLICY_MISSING")
            policy = materialize_web_policy(frozen)
            metadata = _object(row["metadata"])
            web_sequence_capture_integrity(
                metadata, capture_role="after_action", profile=policy.readiness_profile
            )
            presentation = PresentationAssessment.from_dict(metadata.get("presentation"))
            if presentation is None or presentation.is_diagnostic:
                raise ValueError("WEB_CAPABILITY_SOURCE_UNTRUSTED")
            packet = await ManifestBackedPacketLoader(
                manifests=PostgresEvidenceManifestRepository(
                    pool=_RevisionTransactionConnection(connection)
                ),
                artifact_reader=self._artifacts,
            ).load_for_capture(task_id=task_id, capture_id=capture_id)
            if packet.session_id != session_id:
                raise ValueError("WEB_CAPABILITY_MANIFEST_SCOPE_REJECTED")
            snapshots = packet.artifacts_of_type("ui_snapshot")
            if len(snapshots) != 1:
                return None
            snapshot = snapshots[0].decode_json()
            if not isinstance(snapshot, dict) or "web_native_structure" not in snapshot:
                # 历史制品缺失原生 census 时不补造事实或修订。
                return None
            raw = metadata.get("pipeline_metrics", {}).get("web_readiness", {})
            epoch = (
                raw.get("capture_consistency", {}).get("after", {}).get("document_time_origin_ms")
            )
            readiness = read_web_readiness_assessment(
                metadata.get("pipeline_metrics", {}), profile=policy.readiness_profile
            )
            proof = assess_web_local_structure(
                snapshot,
                document_time_origin_ms=epoch,
                stable=bool(readiness and readiness.status.value == "stable"),
            )
            target = stable_protocol_hash(
                {
                    "logical_page_id": policy.identity_policy.logical_id(row["url"]),
                    "identity_policy_hash": frozen.payload_sha256,
                }
            )
            position = {
                key: snapshot.get("viewport", {}).get(key) for key in ("scroll_x", "scroll_y")
            }
            layer = presentation.to_dict()
            state = {
                "position": position,
                "account_state": "unknown",
                "visible_layer": {
                    key: layer.get(key, "unknown")
                    for key in ("topmost_layer_kind", "base_page_visibility", "blocking_scope")
                },
            }
            state_key = stable_protocol_hash(state)
            proof.update(
                task_id=task_id,
                source_agent_run_id=agent_run_id,
                finalization_epoch=row["finalization_epoch"],
                capture_id=capture_id,
                target_id=target,
                state_key=state_key,
                observed_at=row["captured_at"].isoformat(),
                manifest_id=packet.manifest_id,
                manifest_hash=packet.manifest_hash,
                ui_snapshot_artifact_id=snapshots[0].artifact_id,
                material_hash=stable_protocol_hash(
                    {a.artifact_type: a.checksum for a in packet.artifacts}
                ),
                occurrence_id=stable_protocol_hash(
                    {"capture_id": capture_id, "action_execution_id": row["action_execution_id"]}
                ),
                **state,
            )
            existing = await connection.fetchrow(
                "SELECT assessment_payload FROM qc_web_capability_revision WHERE task_id=$1::uuid "
                "AND source_agent_run_id=$2::uuid AND finalization_epoch=$3 AND capture_id=$4::uuid",
                task_id,
                agent_run_id,
                row["finalization_epoch"],
                capture_id,
            )
            if existing:
                previous = _object(existing["assessment_payload"])
                if any(previous.get(key) != value for key, value in proof.items()):
                    raise ValueError("WEB_CAPABILITY_REVISION_SOURCE_DRIFT")
                return previous
            revision = await connection.fetchval(
                "SELECT COALESCE(max(revision),0)+1 FROM qc_web_capability_revision "
                "WHERE task_id=$1::uuid AND source_agent_run_id=$2::uuid AND finalization_epoch=$3 AND target_id=$4 AND state_key=$5",
                task_id,
                agent_run_id,
                row["finalization_epoch"],
                target,
                state_key,
            )
            revision_id = str(
                uuid5(NAMESPACE_URL, "alioth:web-capability:" + stable_protocol_hash(proof))
            )
            proof.update(revision=revision, revision_id=revision_id)
            await connection.execute(
                "INSERT INTO qc_web_capability_revision(revision_id,task_id,source_agent_run_id,finalization_epoch,"
                "capture_id,target_id,state_key,revision,assessment_payload) VALUES($1::uuid,$2::uuid,$3::uuid,$4,$5::uuid,$6,$7,$8,$9::jsonb)",
                revision_id,
                task_id,
                agent_run_id,
                row["finalization_epoch"],
                capture_id,
                target,
                state_key,
                revision,
                json.dumps(proof, ensure_ascii=False),
            )
            return proof


async def read_capture_revision_on_connection(
    connection, *, task_id, agent_run_id, finalization_epoch, capture_id
):
    value = await connection.fetchval(
        "SELECT assessment_payload FROM qc_web_capability_revision WHERE task_id=$1::uuid "
        "AND source_agent_run_id=$2::uuid AND finalization_epoch=$3 AND capture_id=$4::uuid ORDER BY revision DESC LIMIT 1",
        task_id,
        agent_run_id,
        finalization_epoch,
        capture_id,
    )
    return _object(value) if value else None
