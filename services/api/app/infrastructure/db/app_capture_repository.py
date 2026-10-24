"""截图专用 App Capture 与 Bootstrap 绑定的 PostgreSQL 实现。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from services.api.app.application.app_capture_evidence_fields import normalize_session_auth
from services.api.app.application.app_capture_persistence import PersistedAppCapture
from services.api.app.application.app_evidence_manifest import (
    build_pending_app_capture_manifest,
)
from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    SurfaceKind,
    default_capture_state_type,
)
from services.api.app.domain.interaction.validation import optional_text, require_text
from services.api.app.infrastructure.db.app_action_capture_repository import (
    AppActionCapturePersistenceError,
    save_action_capture,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    insert_manifest_on_connection,
)
from services.api.app.infrastructure.db.page_graph_repository import (
    PostgresPageGraphRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

_PAGE_GRAPH_UNSET = object()


class AppCapturePersistenceError(RuntimeError):
    """App Capture 归属或 Bootstrap 乐观锁不满足写入条件。"""


class PostgresAppCaptureRepository:
    """原子写入公共 Capture、App 子表、Artifact 与 Bootstrap 游标。"""

    persists_manifest_atomically = True

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
        page_graph_repository: Any = _PAGE_GRAPH_UNSET,
    ) -> None:
        self._database_url = database_url
        self._pool = pool
        self._page_graph_repository = (
            page_graph_repository
            if page_graph_repository is not _PAGE_GRAPH_UNSET
            else PostgresPageGraphRepository(database_url=database_url, pool=pool)
        )

    async def get_bootstrap_capture(
        self,
        *,
        task_id: str,
        session_id: str,
    ) -> PersistedAppCapture | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    capture.id::text AS capture_id,
                    capture.task_id::text AS task_id,
                    capture.agent_run_id::text AS agent_run_id,
                    capture.surface_session_id AS session_id,
                    capture.capture_role,
                    artifact.id::text AS artifact_id,
                    artifact.artifact_type,
                    artifact.storage_path,
                    artifact.content_type,
                    artifact.checksum,
                    artifact.size_bytes,
                    artifact.metadata AS artifact_metadata,
                    app_capture.width,
                    app_capture.height,
                    app_capture.orientation,
                    app_capture.image_signature,
                    app_capture.package_name,
                    app_capture.activity,
                    bootstrap.row_version AS bootstrap_row_version
                FROM qc_app_bootstrap AS bootstrap
                INNER JOIN qc_capture AS capture
                  ON capture.id = bootstrap.bootstrap_capture_id
                 AND capture.task_id = bootstrap.task_id
                 AND capture.surface_session_id = bootstrap.session_id
                INNER JOIN qc_app_capture AS app_capture
                  ON app_capture.capture_id = capture.id
                INNER JOIN qc_evidence_artifact AS artifact
                  ON artifact.id = app_capture.screenshot_artifact_id
                 AND artifact.task_id = capture.task_id
                 AND artifact.page_capture_id = capture.id
                WHERE bootstrap.task_id = $1::uuid
                  AND bootstrap.session_id = $2
                  AND bootstrap.stage IN ('bootstrap_capturing', 'app_ready')
                  AND capture.surface_kind = 'android'
                  AND capture.capture_role = 'bootstrap'
                  AND capture.capture_status = 'captured'
                """,
                task_id,
                session_id,
            )
        return _parse_app_capture(row) if row is not None else None

    async def get_capture(self, *, capture_id: str) -> PersistedAppCapture | None:
        """按公共 Capture ID 读取截图事实，不依赖 Bootstrap 状态。"""
        require_text(capture_id, "capture_id")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    capture.id::text AS capture_id,
                    capture.task_id::text AS task_id,
                    capture.agent_run_id::text AS agent_run_id,
                    capture.surface_session_id AS session_id,
                    capture.surface_kind,
                    capture.capture_role,
                    capture.parent_capture_id::text AS parent_capture_id,
                    capture.action_execution_id,
                    capture.metadata AS capture_metadata,
                    artifact.id::text AS artifact_id,
                    artifact.artifact_type,
                    artifact.storage_path,
                    artifact.content_type,
                    artifact.checksum,
                    artifact.size_bytes,
                    artifact.metadata AS artifact_metadata,
                    app_capture.width,
                    app_capture.height,
                    app_capture.orientation,
                    app_capture.image_signature,
                    app_capture.package_name,
                    app_capture.activity,
                    -- Bootstrap Capture 经 get_capture 重读时也必须带回检查点版本，
                    -- 否则 PersistedAppCapture 会拒绝 role=bootstrap 且 version 为空。
                    bootstrap.row_version AS bootstrap_row_version
                FROM qc_capture AS capture
                INNER JOIN qc_app_capture AS app_capture
                  ON app_capture.capture_id = capture.id
                INNER JOIN qc_evidence_artifact AS artifact
                  ON artifact.id = app_capture.screenshot_artifact_id
                 AND artifact.task_id = capture.task_id
                 AND artifact.page_capture_id = capture.id
                LEFT JOIN qc_app_bootstrap AS bootstrap
                  ON bootstrap.bootstrap_capture_id = capture.id
                 AND bootstrap.task_id = capture.task_id
                 AND bootstrap.session_id = capture.surface_session_id
                WHERE capture.id = $1::uuid
                  AND capture.surface_kind IN ('android', 'ios')
                  AND capture.capture_status = 'captured'
                """,
                capture_id,
            )
        return _parse_app_capture(row) if row is not None else None

    async def get_capture_artifact(
        self,
        *,
        capture_id: str,
        artifact_type: str,
    ) -> PersistedEvidenceArtifact | None:
        """读取同一 App Capture 下唯一的运行时 Artifact；重复事实一律拒绝。"""

        require_text(capture_id, "capture_id")
        require_text(artifact_type, "artifact_type")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    artifact.id::text AS artifact_id,
                    artifact.artifact_type,
                    artifact.storage_path,
                    artifact.content_type,
                    artifact.checksum,
                    artifact.size_bytes,
                    artifact.metadata AS artifact_metadata
                FROM qc_capture AS capture
                INNER JOIN qc_evidence_artifact AS artifact
                  ON artifact.task_id = capture.task_id
                 AND artifact.page_capture_id = capture.id
                 AND artifact.source = 'app_runtime'
                WHERE capture.id = $1::uuid
                  AND capture.surface_kind IN ('android', 'ios')
                  AND capture.capture_status = 'captured'
                  AND artifact.artifact_type = $2
                ORDER BY artifact.id
                LIMIT 2
                """,
                capture_id,
                artifact_type,
            )
        if len(rows) > 1:
            raise AppCapturePersistenceError("App Capture 存在重复 Artifact 类型")
        if not rows:
            return None
        row = rows[0]
        return PersistedEvidenceArtifact(
            artifact_id=str(row["artifact_id"]),
            artifact_type=str(row["artifact_type"]),
            storage_path=str(row["storage_path"]),
            content_type=str(row["content_type"]),
            checksum=str(row["checksum"]),
            size_bytes=int(row["size_bytes"]),
            metadata=dict(_as_mapping(row["artifact_metadata"], "artifact_metadata")),
            created=False,
        )

    async def get_action_capture(
        self,
        *,
        action_execution_id: str,
        role: str,
    ) -> PersistedAppCapture | None:
        """按动作执行身份和 Capture 角色读取历史事实，兼容旧随机 UUID。"""

        require_text(action_execution_id, "action_execution_id")
        if role not in {"before_action", "after_action"}:
            raise ValueError("App Action Capture role 不受支持")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    capture.id::text AS capture_id,
                    capture.task_id::text AS task_id,
                    capture.agent_run_id::text AS agent_run_id,
                    capture.surface_session_id AS session_id,
                    capture.surface_kind,
                    capture.capture_role,
                    capture.parent_capture_id::text AS parent_capture_id,
                    capture.action_execution_id,
                    capture.metadata AS capture_metadata,
                    artifact.id::text AS artifact_id,
                    artifact.artifact_type,
                    artifact.storage_path,
                    artifact.content_type,
                    artifact.checksum,
                    artifact.size_bytes,
                    artifact.metadata AS artifact_metadata,
                    app_capture.width,
                    app_capture.height,
                    app_capture.orientation,
                    app_capture.image_signature,
                    app_capture.package_name,
                    app_capture.activity,
                    bootstrap.row_version AS bootstrap_row_version
                FROM qc_capture AS capture
                INNER JOIN qc_app_capture AS app_capture
                  ON app_capture.capture_id = capture.id
                INNER JOIN qc_evidence_artifact AS artifact
                  ON artifact.id = app_capture.screenshot_artifact_id
                 AND artifact.task_id = capture.task_id
                 AND artifact.page_capture_id = capture.id
                LEFT JOIN qc_app_bootstrap AS bootstrap
                  ON bootstrap.bootstrap_capture_id = capture.id
                 AND bootstrap.task_id = capture.task_id
                 AND bootstrap.session_id = capture.surface_session_id
                WHERE capture.action_execution_id = $1
                  AND capture.capture_role = $2
                  AND capture.surface_kind IN ('android', 'ios')
                  AND capture.capture_status = 'captured'
                ORDER BY capture.captured_at DESC
                LIMIT 1
                """,
                action_execution_id,
                role,
            )
        return _parse_app_capture(row) if row is not None else None

    async def save_bootstrap_capture(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        capture_id: str,
        screenshot: PersistedEvidenceArtifact,
        width: int,
        height: int,
        orientation: str,
        image_signature: str,
        package_name: str | None,
        activity: str | None,
        device_snapshot: Mapping[str, Any],
        expected_bootstrap_row_version: int,
        workflow_lease_owner: str,
        workflow_lease_token: str,
        manifest: EvidenceManifestReference | None = None,
    ) -> PersistedAppCapture:
        _validate_save_input(
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            capture_id=capture_id,
            screenshot=screenshot,
            width=width,
            height=height,
            orientation=orientation,
            image_signature=image_signature,
            package_name=package_name,
            activity=activity,
            device_snapshot=device_snapshot,
            expected_bootstrap_row_version=expected_bootstrap_row_version,
            workflow_lease_owner=workflow_lease_owner,
            workflow_lease_token=workflow_lease_token,
        )
        metadata = {
            "schema_version": "app-capture.v1",
            "artifact_ids": [screenshot.artifact_id],
            "package_name": package_name,
            "activity": activity,
            "width": width,
            "height": height,
            "orientation": orientation,
            "image_signature": image_signature,
            "state_type": default_capture_state_type("bootstrap"),
            "session_auth": normalize_session_auth(screenshot.metadata.get("session_auth")),
        }
        manifest = manifest or build_pending_app_capture_manifest(
            task_id=task_id,
            session_id=session_id,
            capture_id=capture_id,
            capture_role="bootstrap",
            artifacts=(screenshot,),
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            inserted_capture_id = await connection.fetchval(
                """
                INSERT INTO qc_capture (
                    id, task_id, agent_run_id, surface_kind,
                    surface_session_id, capture_role, capture_status,
                    captured_at, metadata
                )
                SELECT
                    $1::uuid, task.id, run.id, 'android', session.session_id,
                    'bootstrap', 'captured', now(),
                    $8::jsonb || jsonb_build_object(
                        'finalization_epoch', session.finalization_epoch
                    )
                FROM qc_task AS task
                INNER JOIN qc_agent_run AS run
                  ON run.id = $3::uuid
                 AND run.task_id = task.id
                INNER JOIN qc_surface_session AS session
                  ON session.task_id = task.id
                 AND session.session_id = $4
                 AND session.surface_kind = 'android'
                  AND session.status = 'starting'
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
                 AND session.finalization_epoch = task.finalization_epoch
                INNER JOIN qc_app_bootstrap AS bootstrap
                  ON bootstrap.task_id = task.id
                 AND bootstrap.session_id = session.session_id
                 AND bootstrap.stage = 'bootstrap_capturing'
                 AND bootstrap.row_version = $5
                 AND bootstrap.workflow_lease_owner = $6
                 AND bootstrap.workflow_lease_token = $7::uuid
                 AND bootstrap.workflow_lease_expires_at > now()
                 AND bootstrap.bootstrap_capture_id IS NULL
                WHERE task.id = $2::uuid
                  AND task.target_platform = 'app'
                  AND task.status = 'running'
                  AND task.finalization_state = 'open'
                RETURNING id
                """,
                capture_id,
                task_id,
                agent_run_id,
                session_id,
                expected_bootstrap_row_version,
                workflow_lease_owner,
                workflow_lease_token,
                _json_dumps(metadata),
            )
            if inserted_capture_id is None:
                raise AppCapturePersistenceError(
                    "App Capture 的任务、Run、Session、阶段、版本或 Workflow 租约已变化"
                )
            await connection.execute(
                """
                INSERT INTO qc_evidence_artifact (
                    id, task_id, page_capture_id, artifact_type, source,
                    storage_path, content_type, checksum, size_bytes, metadata
                )
                VALUES (
                    $1::uuid, $2::uuid, $3::uuid, 'screenshot', 'app_runtime',
                    $4, $5, $6, $7, $8::jsonb
                )
                """,
                screenshot.artifact_id,
                task_id,
                capture_id,
                screenshot.storage_path,
                screenshot.content_type,
                screenshot.checksum,
                screenshot.size_bytes,
                _json_dumps(screenshot.metadata),
            )
            await connection.execute(
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
                """,
                capture_id,
                screenshot.artifact_id,
                package_name,
                activity,
                width,
                height,
                orientation,
                image_signature,
                _json_dumps(device_snapshot),
            )
            bootstrap_row_version = await connection.fetchval(
                """
                UPDATE qc_app_bootstrap AS bootstrap
                SET
                    bootstrap_capture_id = $3::uuid,
                    row_version = bootstrap.row_version + 1,
                    updated_at = now()
                WHERE bootstrap.task_id = $1::uuid
                  AND bootstrap.session_id = $2
                  AND bootstrap.bootstrap_capture_id IS NULL
                  AND bootstrap.stage = 'bootstrap_capturing'
                  AND bootstrap.row_version = $5
                  AND bootstrap.workflow_lease_owner = $6
                  AND bootstrap.workflow_lease_token = $7::uuid
                  AND bootstrap.workflow_lease_expires_at > now()
                  AND EXISTS (
                    SELECT 1
                    FROM qc_app_capture AS app_capture
                    WHERE app_capture.capture_id = $3::uuid
                      AND app_capture.screenshot_artifact_id = $4::uuid
                  )
                RETURNING bootstrap.row_version
                """,
                task_id,
                session_id,
                capture_id,
                screenshot.artifact_id,
                expected_bootstrap_row_version,
                workflow_lease_owner,
                workflow_lease_token,
            )
            if bootstrap_row_version is None:
                raise AppCapturePersistenceError("App Bootstrap 阶段、版本或 Workflow 租约已变化")
            await insert_manifest_on_connection(connection, manifest=manifest)
            if self._page_graph_repository is not None:
                await self._page_graph_repository.project_capture_from_database_on_connection(
                    connection,
                    capture_id=capture_id,
                )
        return _build_app_capture(
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            capture_id=capture_id,
            screenshot=screenshot,
            width=width,
            height=height,
            orientation=orientation,
            image_signature=image_signature,
            package_name=package_name,
            activity=activity,
            bootstrap_row_version=int(bootstrap_row_version),
        )

    async def save_action_capture(
        self,
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
        action_execution_id: str,
        session_command_sequence: int,
        finalization_epoch: int,
        lease_owner: str,
        lease_token: str,
    ) -> PersistedAppCapture:
        """原子写入动作 Capture，并校验当前 Session Lease 与 Action Execution。"""
        pool = await self._get_pool()
        try:
            return await save_action_capture(
                pool=pool,
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
                page_graph_repository=self._page_graph_repository,
            )
        except AppActionCapturePersistenceError as exc:
            raise AppCapturePersistenceError(str(exc)) from exc

    async def save_observation_capture(
        self,
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
        observation_id: str,
        surface_phase_run_id: str,
        surface_phase_decision_id: str,
        semantic_role: str,
        session_command_sequence: int,
        finalization_epoch: int,
        lease_owner: str,
        lease_token: str,
    ) -> PersistedAppCapture:
        """原子写入重新观察 Capture，不创建或绑定 Action Execution。"""

        pool = await self._get_pool()
        try:
            return await save_action_capture(
                pool=pool,
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
                action_execution_id=None,
                observation_id=observation_id,
                surface_phase_run_id=surface_phase_run_id,
                surface_phase_decision_id=surface_phase_decision_id,
                semantic_role=semantic_role,
                session_command_sequence=session_command_sequence,
                finalization_epoch=finalization_epoch,
                lease_owner=lease_owner,
                lease_token=lease_token,
                page_graph_repository=self._page_graph_repository,
            )
        except AppActionCapturePersistenceError as exc:
            raise AppCapturePersistenceError(str(exc)) from exc

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _parse_app_capture(row: Mapping[str, Any]) -> PersistedAppCapture:
    screenshot = PersistedEvidenceArtifact(
        artifact_id=str(row["artifact_id"]),
        artifact_type=str(row["artifact_type"]),
        storage_path=str(row["storage_path"]),
        content_type=str(row["content_type"]),
        checksum=str(row["checksum"]),
        size_bytes=int(row["size_bytes"]),
        metadata=dict(_as_mapping(row["artifact_metadata"], "artifact_metadata")),
    )
    capture_metadata = (
        _as_mapping(row["capture_metadata"], "capture_metadata")
        if "capture_metadata" in row
        else {}
    )
    raw_artifact_ids = capture_metadata.get("artifact_ids", [str(row["artifact_id"])])
    if not isinstance(raw_artifact_ids, list):
        raise AppCapturePersistenceError("capture_metadata.artifact_ids 必须为数组")
    bootstrap_row_version = row["bootstrap_row_version"]
    return _build_app_capture(
        task_id=str(row["task_id"]),
        agent_run_id=str(row["agent_run_id"]),
        session_id=str(row["session_id"]),
        capture_id=str(row["capture_id"]),
        screenshot=screenshot,
        width=int(row["width"]),
        height=int(row["height"]),
        orientation=str(row["orientation"]),
        image_signature=str(row["image_signature"]),
        package_name=row["package_name"],
        activity=row["activity"],
        role=str(row["capture_role"]),
        surface_kind=(
            SurfaceKind(str(row["surface_kind"])) if "surface_kind" in row else SurfaceKind.ANDROID
        ),
        artifact_ids=tuple(str(item) for item in raw_artifact_ids),
        parent_capture_id=(
            str(row["parent_capture_id"])
            if "parent_capture_id" in row and row["parent_capture_id"] is not None
            else None
        ),
        action_execution_id=(
            str(row["action_execution_id"])
            if "action_execution_id" in row and row["action_execution_id"] is not None
            else None
        ),
        bootstrap_row_version=(
            int(bootstrap_row_version) if bootstrap_row_version is not None else None
        ),
        state_type=(
            str(capture_metadata["state_type"])
            if capture_metadata.get("state_type")
            else default_capture_state_type(str(row["capture_role"]))
        ),
    )


def _build_app_capture(
    *,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    capture_id: str,
    screenshot: PersistedEvidenceArtifact,
    width: int,
    height: int,
    orientation: str,
    image_signature: str,
    package_name: str | None,
    activity: str | None,
    role: str = "bootstrap",
    surface_kind: SurfaceKind = SurfaceKind.ANDROID,
    artifact_ids: tuple[str, ...] | None = None,
    parent_capture_id: str | None = None,
    action_execution_id: str | None = None,
    bootstrap_row_version: int | None = None,
    state_type: str | None = None,
) -> PersistedAppCapture:
    capture = CaptureRef(
        task_id=task_id,
        session_id=session_id,
        capture_id=capture_id,
        surface_kind=surface_kind,
        capture_kind=CaptureKind.APP,
        role=role,
        state_type=state_type or default_capture_state_type(role),
        artifact_ids=artifact_ids or (screenshot.artifact_id,),
        parent_capture_id=parent_capture_id,
        action_execution_id=action_execution_id,
    )
    return PersistedAppCapture(
        capture=capture,
        agent_run_id=agent_run_id,
        screenshot=screenshot,
        width=width,
        height=height,
        orientation=orientation,
        image_signature=image_signature,
        package_name=package_name,
        activity=activity,
        bootstrap_row_version=bootstrap_row_version,
    )


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("App Capture metadata 必须可 JSON 序列化") from exc


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise AppCapturePersistenceError(f"{field_name} 必须为对象")
    return value


def _validate_save_input(
    *,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    capture_id: str,
    screenshot: PersistedEvidenceArtifact,
    width: int,
    height: int,
    orientation: str,
    image_signature: str,
    package_name: str | None,
    activity: str | None,
    device_snapshot: Mapping[str, Any],
    expected_bootstrap_row_version: int,
    workflow_lease_owner: str,
    workflow_lease_token: str,
) -> None:
    for field_name, value in (
        ("task_id", task_id),
        ("agent_run_id", agent_run_id),
        ("session_id", session_id),
        ("capture_id", capture_id),
        ("workflow_lease_owner", workflow_lease_owner),
        ("workflow_lease_token", workflow_lease_token),
        ("image_signature", image_signature),
    ):
        require_text(value, field_name)
    if not isinstance(screenshot, PersistedEvidenceArtifact):
        raise TypeError("screenshot 必须为 PersistedEvidenceArtifact")
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
    forbidden = sorted(
        set(device_snapshot) & {"dom", "html", "visible_text", "ui_tree", "ui_snapshot", "ocr_text"}
    )
    if forbidden:
        raise ValueError("device_snapshot 禁止携带页面正文：" + ", ".join(forbidden))
    if (
        isinstance(expected_bootstrap_row_version, bool)
        or not isinstance(expected_bootstrap_row_version, int)
        or expected_bootstrap_row_version < 0
    ):
        raise ValueError("expected_bootstrap_row_version 必须为非负整数")
