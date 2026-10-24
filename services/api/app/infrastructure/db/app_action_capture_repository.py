"""动作前后 App Capture 的 PostgreSQL 事务实现。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from services.api.app.application.app_capture_evidence_fields import normalize_session_auth
from services.api.app.application.app_capture_persistence import PersistedAppCapture
from services.api.app.application.app_evidence_manifest import (
    build_pending_app_capture_manifest,
)
from services.api.app.application.capture_profiles import APP_MAIN_CAPTURE_ARTIFACT_TYPES
from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    SurfaceKind,
    default_capture_state_type,
)
from services.api.app.domain.interaction.validation import optional_text, require_text
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    insert_manifest_on_connection,
)


class AppActionCapturePersistenceError(RuntimeError):
    """动作 Capture 的 Session、父引用或命令围栏已经失效。"""


async def save_action_capture(
    *,
    pool: Any,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    surface_kind: SurfaceKind,
    capture_id: str,
    artifacts: tuple[PersistedEvidenceArtifact, ...],
    screenshot: PersistedEvidenceArtifact,
    width: int,
    height: int,
    orientation: str,
    image_signature: str,
    package_name: str | None,
    activity: str | None,
    device_snapshot: Mapping[str, Any],
    role: str,
    parent_capture_id: str,
    action_execution_id: str | None,
    session_command_sequence: int,
    finalization_epoch: int,
    lease_owner: str,
    lease_token: str,
    observation_id: str | None = None,
    surface_phase_run_id: str | None = None,
    surface_phase_decision_id: str | None = None,
    semantic_role: str | None = None,
    page_graph_repository: Any | None = None,
) -> PersistedAppCapture:
    """原子写入动作 Capture，并校验当前 Session Lease 与 Action Execution。"""
    _validate_input(
        task_id=task_id,
        agent_run_id=agent_run_id,
        session_id=session_id,
        surface_kind=surface_kind,
        capture_id=capture_id,
        artifacts=artifacts,
        screenshot=screenshot,
        width=width,
        height=height,
        orientation=orientation,
        image_signature=image_signature,
        package_name=package_name,
        activity=activity,
        device_snapshot=device_snapshot,
        role=role,
        parent_capture_id=parent_capture_id,
        action_execution_id=action_execution_id,
        session_command_sequence=session_command_sequence,
        finalization_epoch=finalization_epoch,
        lease_owner=lease_owner,
        lease_token=lease_token,
        observation_id=observation_id,
        surface_phase_run_id=surface_phase_run_id,
        surface_phase_decision_id=surface_phase_decision_id,
        semantic_role=semantic_role,
    )
    metadata = {
        "schema_version": "app-capture.v1",
        "finalization_epoch": finalization_epoch,
        "artifact_ids": [item.artifact_id for item in artifacts],
        "package_name": package_name,
        "activity": activity,
        "width": width,
        "height": height,
        "orientation": orientation,
        "image_signature": image_signature,
        "state_type": default_capture_state_type(role),
        "session_auth": normalize_session_auth(screenshot.metadata.get("session_auth")),
    }
    artifact_contract = screenshot.metadata.get("capture_artifact_contract")
    if isinstance(artifact_contract, Mapping):
        metadata["capture_artifact_contract"] = dict(artifact_contract)
    if role == "observation":
        metadata.update(
            {
                "observation_id": observation_id,
                "surface_phase_run_id": surface_phase_run_id,
                "surface_phase_decision_id": surface_phase_decision_id,
                "semantic_role": semantic_role,
            }
        )
    conflict_clause = (
        """
            ON CONFLICT (id)
            DO UPDATE SET updated_at = now()
            WHERE qc_capture.task_id = EXCLUDED.task_id
              AND qc_capture.surface_session_id = EXCLUDED.surface_session_id
              AND qc_capture.parent_capture_id = EXCLUDED.parent_capture_id
              AND qc_capture.action_execution_id IS NOT DISTINCT FROM EXCLUDED.action_execution_id
              AND qc_capture.capture_role = EXCLUDED.capture_role
              AND qc_capture.capture_status = 'captured'
        """
        if role == "observation"
        else """
            ON CONFLICT (action_execution_id, capture_role)
                WHERE action_execution_id IS NOT NULL
                  AND capture_role IN ('before_action', 'after_action')
            DO UPDATE SET updated_at = now()
            WHERE qc_capture.task_id = EXCLUDED.task_id
              AND qc_capture.surface_session_id = EXCLUDED.surface_session_id
              AND qc_capture.parent_capture_id = EXCLUDED.parent_capture_id
              AND qc_capture.action_execution_id = EXCLUDED.action_execution_id
              AND qc_capture.capture_role = EXCLUDED.capture_role
              AND qc_capture.capture_status = 'captured'
        """
    )
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))",
            f"task-finalization:{task_id}",
        )
        inserted_capture_id = await connection.fetchval(
            f"""
            INSERT INTO qc_capture (
                id, task_id, agent_run_id, surface_kind,
                surface_session_id, parent_capture_id,
                action_execution_id, capture_role,
                session_command_sequence, capture_status,
                captured_at, metadata
            )
            SELECT
                $1::uuid, task.id, run.id, session.surface_kind,
                session.session_id, parent.id, $7,
                $8, $9, 'captured', now(), $11::jsonb
            FROM qc_task AS task
            INNER JOIN qc_agent_run AS run
              ON run.id = $3::uuid
             AND run.task_id = task.id
            INNER JOIN qc_surface_session AS session
              ON session.task_id = task.id
             AND session.session_id = $4
             AND session.surface_kind = $5
             AND session.status = 'active'
             AND session.command_sequence = $9
             AND session.finalization_epoch = $10
             AND session.lease_owner = $12
             AND session.lease_token = $13::uuid
             AND session.lease_expires_at > now()
             AND (
                session.device_lease_id IS NULL
                OR EXISTS (
                    SELECT 1
                    FROM qc_device_lease AS device_lease
                    WHERE device_lease.lease_id = session.device_lease_id
                      AND device_lease.fencing_token =
                          session.device_lease_fencing_token
                      AND device_lease.state IN ('claimed', 'active')
                      AND device_lease.expires_at > now()
                )
             )
            INNER JOIN qc_capture AS parent
              ON parent.id = $6::uuid
             AND parent.task_id = task.id
             AND parent.surface_session_id = session.session_id
             AND parent.surface_kind = session.surface_kind
             AND parent.capture_status = 'captured'
            LEFT JOIN qc_action_execution AS action
              ON action.action_execution_id = $7
             AND action.task_id = task.id
             AND action.session_id = session.session_id
             AND action.command_sequence = session.command_sequence
             AND (
                ($8 = 'before_action'
                 AND action.status = 'planned'
                 AND action.requested_source_capture_id = parent.id)
                OR
                ($8 = 'after_action'
                 AND action.status IN (
                    'evidence_pending', 'blocked', 'failed', 'timed_out'
                 )
                 AND action.source_capture_id = parent.id)
             )
            WHERE task.id = $2::uuid
              AND task.target_platform = 'app'
              AND task.status = 'running'
              AND task.finalization_state = 'open'
              AND (
                ($8 = 'observation' AND $7 IS NULL)
                OR (
                    $8 IN ('before_action', 'after_action')
                    AND $7 IS NOT NULL
                    AND action.action_execution_id IS NOT NULL
                )
              )
            {conflict_clause}
            RETURNING id
            """,
            capture_id,
            task_id,
            agent_run_id,
            session_id,
            surface_kind.value,
            parent_capture_id,
            action_execution_id,
            role,
            session_command_sequence,
            finalization_epoch,
            _json_dumps(metadata),
            lease_owner,
            lease_token,
        )
        if inserted_capture_id is None:
            raise AppActionCapturePersistenceError(
                "App Action Capture 的 Session Lease、父 Capture、Action 或命令版本已变化"
            )
        canonical_capture_id = str(inserted_capture_id)
        existing_artifacts = await connection.fetch(
            """
            SELECT
                id::text AS artifact_id,
                task_id::text AS task_id,
                page_capture_id::text AS page_capture_id,
                artifact_type,
                source,
                storage_path,
                content_type,
                checksum,
                size_bytes,
                metadata
            FROM qc_evidence_artifact
            WHERE task_id = $1::uuid
              AND page_capture_id = $2::uuid
              AND source = 'app_runtime'
            FOR UPDATE
            """,
            task_id,
            canonical_capture_id,
        )
        existing_by_type = {str(row["artifact_type"]): row for row in existing_artifacts}
        resolved_artifact_ids: dict[str, str] = {}
        resolved_artifacts: dict[str, PersistedEvidenceArtifact] = {}
        for artifact in artifacts:
            existing = existing_by_type.get(artifact.artifact_type)
            if existing is not None:
                if not _artifact_identity_matches(
                    existing,
                    task_id=task_id,
                    page_capture_id=canonical_capture_id,
                    artifact=artifact,
                ):
                    raise AppActionCapturePersistenceError(
                        f"历史 Artifact {existing['artifact_id']} 与当前事实不一致，拒绝覆盖"
                    )
                existing_artifact = _artifact_from_row(existing)
                resolved_artifact_ids[artifact.artifact_type] = existing_artifact.artifact_id
                resolved_artifacts[artifact.artifact_type] = existing_artifact
                continue
            artifact_id = await connection.fetchval(
                """
                INSERT INTO qc_evidence_artifact (
                    id, task_id, page_capture_id, artifact_type, source,
                    storage_path, content_type, checksum, size_bytes, metadata
                )
                VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4, 'app_runtime',
                    $5, $6, $7, $8, $9::jsonb
                )
                ON CONFLICT (id) DO UPDATE SET
                    id = qc_evidence_artifact.id
                WHERE qc_evidence_artifact.task_id = EXCLUDED.task_id
                  AND qc_evidence_artifact.page_capture_id = EXCLUDED.page_capture_id
                  AND qc_evidence_artifact.artifact_type = EXCLUDED.artifact_type
                  AND qc_evidence_artifact.storage_path = EXCLUDED.storage_path
                  AND qc_evidence_artifact.content_type = EXCLUDED.content_type
                  AND qc_evidence_artifact.checksum = EXCLUDED.checksum
                  AND qc_evidence_artifact.size_bytes = EXCLUDED.size_bytes
                  AND qc_evidence_artifact.metadata = EXCLUDED.metadata
                RETURNING id
                """,
                artifact.artifact_id,
                task_id,
                canonical_capture_id,
                artifact.artifact_type,
                artifact.storage_path,
                artifact.content_type,
                artifact.checksum,
                artifact.size_bytes,
                _json_dumps(artifact.metadata),
            )
            if artifact_id is None:
                raise AppActionCapturePersistenceError(
                    f"Artifact {artifact.artifact_id} 已存在但内容或归属不一致"
                )
            resolved_artifact_id = str(artifact_id)
            resolved_artifact_ids[artifact.artifact_type] = resolved_artifact_id
            resolved_artifacts[artifact.artifact_type] = replace(
                artifact,
                artifact_id=resolved_artifact_id,
            )
        resolved_screenshot_id = resolved_artifact_ids[screenshot.artifact_type]
        app_capture_id = await connection.fetchval(
            """
            INSERT INTO qc_app_capture (
                capture_id, screenshot_artifact_id,
                package_name, activity, width, height,
                orientation, image_signature, device_snapshot
            )
            VALUES (
                $1::uuid, $2::uuid, $3, $4, $5, $6,
                $7, $8, $9::jsonb
            )
            ON CONFLICT (capture_id) DO UPDATE SET
                capture_id = qc_app_capture.capture_id
            WHERE qc_app_capture.screenshot_artifact_id = EXCLUDED.screenshot_artifact_id
              AND qc_app_capture.package_name IS NOT DISTINCT FROM EXCLUDED.package_name
              AND qc_app_capture.activity IS NOT DISTINCT FROM EXCLUDED.activity
              AND qc_app_capture.width = EXCLUDED.width
              AND qc_app_capture.height = EXCLUDED.height
              AND qc_app_capture.orientation = EXCLUDED.orientation
              AND qc_app_capture.image_signature = EXCLUDED.image_signature
              AND qc_app_capture.device_snapshot = EXCLUDED.device_snapshot
            RETURNING capture_id
            """,
            canonical_capture_id,
            resolved_screenshot_id,
            package_name,
            activity,
            width,
            height,
            orientation,
            image_signature,
            _json_dumps(device_snapshot),
        )
        if app_capture_id is None:
            raise AppActionCapturePersistenceError(
                f"App Capture {canonical_capture_id} 已存在但截图或设备事实不一致"
            )
        committed_manifest = build_pending_app_capture_manifest(
            task_id=task_id,
            session_id=session_id,
            capture_id=canonical_capture_id,
            capture_role=role,
            artifacts=tuple(resolved_artifacts[item.artifact_type] for item in artifacts),
        )
        await insert_manifest_on_connection(connection, manifest=committed_manifest)
        if page_graph_repository is not None:
            await page_graph_repository.project_capture_on_connection(
                connection,
                task_id=task_id,
                capture_id=canonical_capture_id,
                agent_run_id=agent_run_id,
                surface_kind=surface_kind.value,
                session_id=session_id,
                finalization_epoch=finalization_epoch,
                command_sequence=session_command_sequence,
                capture_role=role,
                metadata=metadata,
                page_type=None,
                page_state=None,
                captured_at=None,
            )
    capture = CaptureRef(
        task_id=task_id,
        session_id=session_id,
        capture_id=canonical_capture_id,
        surface_kind=surface_kind,
        capture_kind=CaptureKind.APP,
        role=role,
        state_type=default_capture_state_type(role),
        artifact_ids=tuple(resolved_artifact_ids[item.artifact_type] for item in artifacts),
        parent_capture_id=parent_capture_id,
        action_execution_id=action_execution_id,
    )
    return PersistedAppCapture(
        capture=capture,
        agent_run_id=agent_run_id,
        screenshot=resolved_artifacts[screenshot.artifact_type],
        width=width,
        height=height,
        orientation=orientation,
        image_signature=image_signature,
        package_name=package_name,
        activity=activity,
    )


def _validate_input(
    *,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    surface_kind: SurfaceKind,
    capture_id: str,
    artifacts: tuple[PersistedEvidenceArtifact, ...],
    screenshot: PersistedEvidenceArtifact,
    width: int,
    height: int,
    orientation: str,
    image_signature: str,
    package_name: str | None,
    activity: str | None,
    device_snapshot: Mapping[str, Any],
    role: str,
    parent_capture_id: str,
    action_execution_id: str | None,
    session_command_sequence: int,
    finalization_epoch: int,
    lease_owner: str,
    lease_token: str,
    observation_id: str | None = None,
    surface_phase_run_id: str | None = None,
    surface_phase_decision_id: str | None = None,
    semantic_role: str | None = None,
) -> None:
    for field_name, value in (
        ("task_id", task_id),
        ("agent_run_id", agent_run_id),
        ("session_id", session_id),
        ("capture_id", capture_id),
        ("image_signature", image_signature),
        ("parent_capture_id", parent_capture_id),
        ("lease_owner", lease_owner),
        ("lease_token", lease_token),
    ):
        require_text(value, field_name)
    if role == "observation":
        if action_execution_id is not None:
            raise ValueError("Observation Capture 不得绑定 Action Execution")
        for field_name, value in (
            ("observation_id", observation_id),
            ("surface_phase_run_id", surface_phase_run_id),
            ("surface_phase_decision_id", surface_phase_decision_id),
            ("semantic_role", semantic_role),
        ):
            require_text(value, field_name)
    else:
        require_text(action_execution_id, "action_execution_id")
    if not isinstance(surface_kind, SurfaceKind) or surface_kind is SurfaceKind.WEB:
        raise ValueError("surface_kind 必须为 App Surface")
    if role not in {"before_action", "after_action", "observation"}:
        raise ValueError("role 必须为 before_action、after_action 或 observation")
    if not artifacts or any(not isinstance(item, PersistedEvidenceArtifact) for item in artifacts):
        raise ValueError("artifacts 必须为非空持久化 Artifact 集合")
    artifact_ids = tuple(item.artifact_id for item in artifacts)
    artifact_types = tuple(item.artifact_type for item in artifacts)
    if len(set(artifact_ids)) != len(artifact_ids) or len(set(artifact_types)) != len(
        artifact_types
    ):
        raise ValueError("App Action Capture Artifact 不能重复")
    if set(artifact_types) - set(APP_MAIN_CAPTURE_ARTIFACT_TYPES):
        raise ValueError("App Action Capture Artifact 类型不受支持")
    if screenshot.artifact_id not in artifact_ids:
        raise ValueError("screenshot 必须属于 artifacts")
    if (
        screenshot.artifact_type != "screenshot"
        or screenshot.content_type != "image/png"
        or screenshot.size_bytes <= 0
        or screenshot.checksum != image_signature
    ):
        raise ValueError("Screenshot Artifact 元数据不合法")
    for field_name, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field_name} 必须为正整数")
    if orientation not in {"portrait", "landscape", "unknown"}:
        raise ValueError("orientation 不受支持")
    optional_text(package_name, "package_name")
    optional_text(activity, "activity")
    if not isinstance(device_snapshot, Mapping):
        raise ValueError("device_snapshot 必须为对象")
    for field_name, value in (
        ("session_command_sequence", session_command_sequence),
        ("finalization_epoch", finalization_epoch),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field_name} 必须为非负整数")
    if session_command_sequence == 0:
        raise ValueError("session_command_sequence 必须为正整数")


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _artifact_identity_matches(
    existing: Mapping[str, Any],
    *,
    task_id: str,
    page_capture_id: str,
    artifact: PersistedEvidenceArtifact,
) -> bool:
    return (
        str(existing.get("task_id")) == task_id
        and str(existing.get("page_capture_id")) == page_capture_id
        and str(existing.get("source")) == "app_runtime"
        and str(existing.get("artifact_type")) == artifact.artifact_type
        and str(existing.get("content_type")) == artifact.content_type
        and str(existing.get("checksum")) == artifact.checksum
        and int(existing.get("size_bytes")) == artifact.size_bytes
        and _json_mapping(existing.get("metadata")) == dict(artifact.metadata)
    )


def _artifact_from_row(row: Mapping[str, Any]) -> PersistedEvidenceArtifact:
    """返回数据库事实，保留历史 UUID4 Artifact 的 ID 与存储路径。"""
    return PersistedEvidenceArtifact(
        artifact_id=str(row["artifact_id"]),
        artifact_type=str(row["artifact_type"]),
        storage_path=str(row["storage_path"]),
        content_type=str(row["content_type"]),
        checksum=str(row["checksum"]),
        size_bytes=int(row["size_bytes"]),
        metadata=_json_mapping(row.get("metadata")),
        created=False,
    )


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}
