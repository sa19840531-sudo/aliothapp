"""页面采集产物持久化编排。"""

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from services.api.app.application.evidence_manifest_writer import (
    build_pending_page_capture_manifest,
    manifest_session_id,
)
from services.api.app.application.evidence_presentation import (
    DECLARED_DIAGNOSTIC_KEY,
    DECLARED_OBSTRUCTED_KEY,
    DECLARED_SESSION_AUTH_KEY,
    SESSION_AUTH_VALUES,
    presentation_from_capture_result,
)
from services.api.app.application.h5_session_journey import assert_h5_journeys_active
from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.domain.evidence.artifact_identity import (
    validate_evidence_artifact_identity,
)
from services.api.app.domain.evidence.presentation import PRESENTATION_METADATA_KEY
from services.api.app.domain.exploration.h5_logical_surface import logical_surface_is_effective
from services.api.app.domain.interaction import CaptureRef, SurfaceKind

SUPPORTED_CAPTURE_ARTIFACT_TYPES = frozenset(
    {
        "screenshot",
        "dom",
        "visible_text",
        "har",
        "console_log",
        "action_trace",
        "ui_snapshot",
        "media_state",
        "checkpoint_observation",
        "temporal_trace",
        "page_coverage",
    }
)


@dataclass(frozen=True)
class PersistedEvidenceArtifact:
    """已写入证据存储的产物元数据。"""

    artifact_id: str
    artifact_type: str
    storage_path: str
    content_type: str
    checksum: str
    size_bytes: int
    metadata: dict[str, Any]
    # 同一业务身份重试时为 False，补偿逻辑不能删除这个已有对象。
    created: bool = True


@dataclass(frozen=True)
class PersistedPageCapture:
    """已写入数据库的页面采集结果。"""

    page_capture_id: str
    page_seq: int
    artifacts: list[PersistedEvidenceArtifact]
    # E07/E08：随采集同事务提交的 v1 pending Evidence Manifest；
    # 无 DB 或零附件路径为 None。
    manifest: EvidenceManifestReference | None = None


@dataclass(frozen=True)
class PersistedPageRecoveryCapture:
    """可用于动作恢复的页面采集事实。"""

    capture: CaptureRef
    artifacts: tuple[PersistedEvidenceArtifact, ...]
    manifest_quality_status: str | None


class PageCaptureRecoveryError(RuntimeError):
    """H5 动作恢复引用的页面证据不可信。"""

    retryable = False

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class EvidenceArtifactStore(Protocol):
    """大体积证据产物存储契约。"""

    async def store(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        artifact_id: str,
        artifact_type: str,
        content: Any,
    ) -> PersistedEvidenceArtifact: ...

    async def delete(self, storage_path: str) -> None: ...

    async def read(self, storage_path: str) -> bytes: ...


class PageCaptureRepository(Protocol):
    """页面采集与证据元数据仓储契约。"""

    async def save_capture(self, **kwargs: Any) -> int: ...

    async def get_recovery_capture(
        self,
        *,
        capture_id: str,
    ) -> PersistedPageRecoveryCapture | None: ...

    async def get_action_recovery_capture(
        self,
        *,
        action_execution_id: str,
        role: str,
    ) -> PersistedPageRecoveryCapture | None: ...


class PageCaptureRecoveryService:
    """验证 H5 动作历史 Capture，恢复过程不得重新采集或执行动作。"""

    def __init__(
        self,
        *,
        repository: PageCaptureRepository,
        artifact_store: EvidenceArtifactStore,
    ) -> None:
        self._repository = repository
        self._artifact_store = artifact_store

    async def get_verified_capture(
        self,
        *,
        capture_id: str,
    ) -> PersistedPageRecoveryCapture | None:
        persisted = await self._repository.get_recovery_capture(capture_id=capture_id)
        if persisted is None:
            return None
        return await self._verify(persisted)

    async def get_verified_action_capture(
        self,
        *,
        action_execution_id: str,
        role: str,
    ) -> PersistedPageRecoveryCapture | None:
        persisted = await self._repository.get_action_recovery_capture(
            action_execution_id=action_execution_id,
            role=role,
        )
        if persisted is None:
            return None
        if (
            persisted.capture.action_execution_id != action_execution_id
            or persisted.capture.role != role
        ):
            raise PageCaptureRecoveryError(
                "H5 动作恢复 Capture 身份不一致",
                error_code="H5_ACTION_RECOVERY_CAPTURE_IDENTITY_MISMATCH",
            )
        return await self._verify(persisted)

    async def get_verified_observation_capture(
        self,
        *,
        capture_id: str,
    ) -> PersistedPageRecoveryCapture | None:
        """H5 现场识别可读取已提交的 pending 清单，但不授予正式证据资格。"""
        persisted = await self._repository.get_recovery_capture(capture_id=capture_id)
        if persisted is None:
            return None
        if persisted.capture.capture_id != capture_id:
            raise PageCaptureRecoveryError(
                "H5 现场识别 Capture 身份不一致",
                error_code="H5_ACTION_RECOVERY_CAPTURE_IDENTITY_MISMATCH",
            )
        return await self._verify(persisted, observation_only=True)

    async def _verify(
        self,
        persisted: PersistedPageRecoveryCapture,
        *,
        observation_only: bool = False,
    ) -> PersistedPageRecoveryCapture:
        capture = persisted.capture
        if (
            capture.surface_kind is not SurfaceKind.WEB
            or not capture.session_id.startswith(
                ("h5:",) if observation_only else ("h5:", "web-v2:")
            )
            or persisted.manifest_quality_status not in (
                ("pass", "pending") if observation_only else ("pass",)
            )
            or not persisted.artifacts
            or tuple(item.artifact_id for item in persisted.artifacts) != capture.artifact_ids
        ):
            raise PageCaptureRecoveryError(
                "H5 动作恢复 Capture 未通过可信证据校验",
                error_code="H5_ACTION_RECOVERY_CAPTURE_INVALID",
            )
        for artifact in persisted.artifacts:
            try:
                content = await self._artifact_store.read(artifact.storage_path)
            except Exception as exc:  # noqa: BLE001 - 统一为恢复失败合同
                raise PageCaptureRecoveryError(
                    "H5 动作恢复 Artifact 不可读取",
                    error_code="H5_ACTION_RECOVERY_ARTIFACT_UNREADABLE",
                ) from exc
            if not isinstance(content, (bytes, bytearray)):
                raise PageCaptureRecoveryError(
                    "H5 动作恢复 Artifact 不是二进制内容",
                    error_code="H5_ACTION_RECOVERY_ARTIFACT_INVALID",
                )
            raw = bytes(content)
            checksum = f"sha256:{hashlib.sha256(raw).hexdigest()}"
            if len(raw) != artifact.size_bytes or checksum != artifact.checksum:
                raise PageCaptureRecoveryError(
                    "H5 动作恢复 Artifact 与持久化事实不一致",
                    error_code="H5_ACTION_RECOVERY_ARTIFACT_CHECKSUM_MISMATCH",
                )
        return persisted


class PageCapturePersistenceService:
    """协调证据文件写入与页面采集数据库记录。"""

    _SUPPORTED_ARTIFACT_TYPES = SUPPORTED_CAPTURE_ARTIFACT_TYPES

    def __init__(
        self,
        *,
        repository: PageCaptureRepository,
        artifact_store: EvidenceArtifactStore,
        web_catalog_identity: object | None = None,
    ) -> None:
        self._repository = repository
        self._artifact_store = artifact_store
        self._web_catalog_identity = web_catalog_identity

    async def record_ad_page_detection(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        page_capture_id: str,
        decision_source: str,
        fallback_reason: str | None,
        model_page_type: str | None,
        model_confidence: float | None,
    ) -> bool:
        """在隔离页证据提交后记录模型优先、谱系兜底的页型审计。"""

        identity = self._web_catalog_identity
        if identity is None:
            return False
        catalog_version = getattr(identity, "catalog_version", None)
        catalog_sha256 = getattr(identity, "catalog_sha256", None)
        if not isinstance(catalog_version, str) or not isinstance(catalog_sha256, str):
            return False
        update = getattr(self._repository, "update_detection_result", None)
        if not callable(update):
            return False
        return bool(
            await update(
                task_id=task_id,
                agent_run_id=agent_run_id,
                page_capture_id=page_capture_id,
                page_type="ad_page",
                page_state="loaded",
                detection_status="recognized",
                confidence=(model_confidence if decision_source == "model" else 1.0),
                catalog_version=catalog_version,
                catalog_sha256=catalog_sha256,
                confidence_source=decision_source,
                model_page_type=model_page_type,
                model_confidence=model_confidence,
                decision_source=decision_source,
                fallback_reason=fallback_reason,
            )
        )

    async def persist(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        result: Any,
        parent_page_capture_id: str | None = None,
        capture_role: str = "primary",
        capture_purpose: str | None = None,
        direct_page_type: str | None = None,
        capture_url: str | None = None,
        requested_url: str | None = None,
        inherit_parent_semantics: bool | None = None,
        surface_session_id: str | None = None,
        action_execution_id: str | None = None,
        session_command_sequence: int | None = None,
        finalization_epoch: int | None = None,
    ) -> PersistedPageCapture:
        if str(surface_session_id or "").startswith("h5:"):
            assert_h5_journeys_active()
        if direct_page_type is not None and not (
            direct_page_type == "ad_page"
            and capture_purpose == "web_ad_page"
            and capture_role == "supplemental"
            and inherit_parent_semantics is False
        ):
            raise ValueError("直接页面类型仅允许隔离广告证据签发 ad_page")
        if (capture_url is not None or requested_url is not None) and not (
            direct_page_type == "ad_page" and capture_purpose == "web_ad_page"
        ):
            raise ValueError("页面 URL 覆盖仅允许隔离广告证据")
        if (surface_session_id is None) != (session_command_sequence is None):
            raise ValueError("Surface Session 与命令序列必须同时提供")
        if (surface_session_id is None) != (finalization_epoch is None):
            raise ValueError("Surface Session 与 Finalization Epoch 必须同时提供")
        page_capture_id = str(uuid4())
        stored_artifacts: list[PersistedEvidenceArtifact] = []
        artifact_entries = [
            (artifact, str(uuid4()))
            for artifact in result.artifacts
            if artifact.artifact_type in self._SUPPORTED_ARTIFACT_TYPES
        ]
        try:
            for artifact, artifact_id in artifact_entries:
                content = artifact.content
                if artifact.artifact_type == "page_coverage" and isinstance(content, dict):
                    content = {
                        **content,
                        "associated_page_capture_id": page_capture_id,
                        "associated_screenshot_artifact_ids": (
                            _coverage_screenshot_artifact_ids(
                                artifact_entries=artifact_entries,
                                coverage_content=content,
                            )
                        ),
                    }
                stored_artifacts.append(
                    await self._artifact_store.store(
                        task_id=task_id,
                        page_capture_id=page_capture_id,
                        artifact_id=artifact_id,
                        artifact_type=artifact.artifact_type,
                        content=content,
                    )
                )

            _validate_stored_artifacts(stored_artifacts)

            # 展示层评估随采集行与清单一起落库：最上层是谁、底页是否可见、是否经过诊断
            # 操作；同一 Surface Session 内更早采集的诊断操作先继承进来，再写进清单，
            # 保证清单里的来源事实与采集行一致。
            presentation = presentation_from_capture_result(
                artifacts=result.artifacts,
                action_trace=result.action_trace,
            )
            lineage_loader = getattr(self._repository, "load_session_diagnostic_lineage", None)
            if surface_session_id is not None and lineage_loader is not None:
                presentation = presentation.with_diagnostic_operations(
                    await lineage_loader(
                        task_id=task_id,
                        surface_session_id=surface_session_id,
                        document_epoch_ms=presentation.document_epoch_ms,
                    )
                )
            presentation_payload = presentation.to_dict()
            declared_evidence_flags = await _h5_declared_evidence_flags(
                repository=self._repository,
                task_id=task_id,
                surface_session_id=surface_session_id,
                presentation=presentation,
            )
            artifact_metadata = {
                PRESENTATION_METADATA_KEY: presentation_payload,
                **declared_evidence_flags,
            }

            # E07/E08/E09：pending 清单与采集行、附件行同事务原子提交。
            manifest = _build_pending_manifest(
                task_id=task_id,
                agent_run_id=agent_run_id,
                page_capture_id=page_capture_id,
                capture_role=capture_role,
                surface_session_id=surface_session_id,
                artifacts=stored_artifacts,
                artifact_metadata=artifact_metadata,
            )
            capture_evidence_flags = dict(declared_evidence_flags)
            if isinstance(surface_session_id, str) and surface_session_id.startswith("h5:"):
                snapshots = [
                    artifact.content for artifact in result.artifacts
                    if artifact.artifact_type == "ui_snapshot"
                    and isinstance(artifact.content, Mapping)
                ]
                if len(snapshots) == 1:
                    snapshot = snapshots[0]
                    logical = snapshot.get("logical_surface")
                    if (
                        isinstance(logical, Mapping)
                        and logical.get("protocol_version") == "logical_surface.v1"
                        and logical_surface_is_effective(logical)
                        and logical.get("evidence_quality") == "high"
                        and not logical.get("shadow")
                        and not logical.get("degraded_reasons")
                        and not snapshot.get("collection_error")
                        and not snapshot.get("capture_degraded")
                        and not snapshot.get("budget_truncated")
                    ):
                        # Before/Probe 也保留本帧结构身份，不继承父页类型或制造覆盖结果。
                        capture_evidence_flags["logical_surface"] = dict(logical)
            save_kwargs = {
                "task_id": task_id,
                "agent_run_id": agent_run_id,
                "page_capture_id": page_capture_id,
                "presentation": presentation_payload,
                "declared_evidence_flags": capture_evidence_flags,
                "url": capture_url or result.final_url or "about:blank",
                "mode": result.mode,
                "reachable": result.reachable,
                "title": result.title,
                "status_code": result.status_code,
                "action_log": result.action_trace,
                "artifacts": stored_artifacts,
                "metrics": dict(getattr(result, "metrics", {}) or {}),
                "parent_page_capture_id": parent_page_capture_id,
                "capture_role": capture_role,
                "direct_page_type": direct_page_type,
                "requested_url": requested_url,
                "surface_session_id": surface_session_id,
                "action_execution_id": action_execution_id,
                "session_command_sequence": session_command_sequence,
                "finalization_epoch": finalization_epoch,
                "inherit_parent_semantics": (
                    _inherits_parent_semantics(result)
                    if inherit_parent_semantics is None
                    else inherit_parent_semantics
                ),
                "manifest": manifest,
            }
            if capture_purpose is not None:
                save_kwargs["capture_purpose"] = capture_purpose
            if str(surface_session_id or "").startswith("h5:"):
                assert_h5_journeys_active()
            try:
                page_seq = await self._repository.save_capture(**save_kwargs)
            except RuntimeError as exc:
                if not _should_retry_without_surface_binding(
                    error=exc,
                    surface_session_id=surface_session_id,
                    action_execution_id=action_execution_id,
                    session_command_sequence=session_command_sequence,
                    finalization_epoch=finalization_epoch,
                ):
                    raise
                # 运行态绑定被剥离后，清单的 session 身份同步退回运行哨兵。
                manifest = _build_pending_manifest(
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    page_capture_id=page_capture_id,
                    capture_role=capture_role,
                    surface_session_id=None,
                    artifacts=stored_artifacts,
                    artifact_metadata=artifact_metadata,
                )
                page_seq = await self._repository.save_capture(
                    **{
                        **save_kwargs,
                        "surface_session_id": None,
                        "action_execution_id": None,
                        "session_command_sequence": None,
                        "finalization_epoch": None,
                        "manifest": manifest,
                    }
                )
        except BaseException:
            # asyncio 超时会以 CancelledError 打断持久化。已成功写入的对象必须
            # 尽力清理，且单个删除也要有界，避免清理再次阻塞页面 Frontier。
            for artifact in stored_artifacts:
                if not artifact.created:
                    continue
                try:
                    await asyncio.wait_for(
                        self._artifact_store.delete(artifact.storage_path),
                        timeout=5,
                    )
                except BaseException:  # noqa: BLE001  # 清理失败不得遮蔽原异常
                    continue
            raise

        return PersistedPageCapture(
            page_capture_id=page_capture_id,
            page_seq=page_seq,
            artifacts=stored_artifacts,
            manifest=manifest,
        )


async def _h5_declared_evidence_flags(
    *,
    repository: PageCaptureRepository,
    task_id: str,
    surface_session_id: str | None,
    presentation: Any,
) -> dict[str, Any]:
    """只为 H5 会话声明正式证据字段，避免改变 Web/App 的采集合同。"""

    if not isinstance(surface_session_id, str) or not surface_session_id.startswith("h5:"):
        return {}
    session_auth = "unknown"
    auth_loader = getattr(repository, "load_h5_session_auth", None)
    if callable(auth_loader):
        loaded = str(
            await auth_loader(
                task_id=task_id,
                surface_session_id=surface_session_id,
            )
            or "unknown"
        ).strip().casefold()
        if loaded in SESSION_AUTH_VALUES:
            session_auth = loaded
    flags: dict[str, Any] = {
        DECLARED_SESSION_AUTH_KEY: session_auth,
        DECLARED_OBSTRUCTED_KEY: bool(presentation.occluding_layer_present),
    }
    diagnostic = list(
        dict.fromkeys(
            operation.kind
            for operation in presentation.diagnostic_operations
            if operation.kind
        )
    )
    if diagnostic:
        flags[DECLARED_DIAGNOSTIC_KEY] = diagnostic
    return flags


def _coverage_screenshot_artifact_ids(
    *, artifact_entries: list[tuple[Any, str]], coverage_content: dict[str, Any]
) -> list[str]:
    """覆盖清单只引用同协议截图，其他视觉视口图保持独立证据身份。"""

    purpose = {
        "web-full-page-segment-manifest.v1": "web_full_page_segment",
        "web-scrolled-viewport-manifest.v1": "web_scrolled_viewport",
    }.get(coverage_content.get("protocol_version"))
    h5_scroll_role = (
        "h5_vertical_scroll_collection"
        if coverage_content.get("capture_role") == "h5_vertical_scroll_collection"
        else None
    )
    return [
        artifact_id
        for artifact, artifact_id in artifact_entries
        if artifact.artifact_type == "screenshot"
        and (
            h5_scroll_role is None
            or (
                isinstance(artifact.content, dict)
                and artifact.content.get("capture_role") == h5_scroll_role
            )
        )
        and (
            purpose is None
            or (
                isinstance(artifact.content, dict)
                and artifact.content.get("purpose") == purpose
            )
        )
    ]


def _validate_stored_artifacts(
    artifacts: list[PersistedEvidenceArtifact] | tuple[PersistedEvidenceArtifact, ...],
) -> None:
    """在进入正式仓储前统一校验证据附件身份。"""
    for artifact in artifacts:
        if not isinstance(artifact, PersistedEvidenceArtifact):
            raise TypeError("Evidence Store 未返回 PersistedEvidenceArtifact")
        validate_evidence_artifact_identity(
            storage_path=artifact.storage_path,
            checksum=artifact.checksum,
            size_bytes=artifact.size_bytes,
        )


def _build_pending_manifest(
    *,
    task_id: str,
    agent_run_id: str,
    page_capture_id: str,
    capture_role: str,
    surface_session_id: str | None,
    artifacts: list[PersistedEvidenceArtifact],
    artifact_metadata: dict[str, Any] | None = None,
) -> EvidenceManifestReference | None:
    """零附件采集没有可清单化的证据身份，返回 None（契约要求至少一个附件）。"""
    if not artifacts:
        return None
    return build_pending_page_capture_manifest(
        task_id=task_id,
        session_id=manifest_session_id(surface_session_id, agent_run_id=agent_run_id),
        capture_id=page_capture_id,
        capture_role=capture_role,
        artifacts=artifacts,
        artifact_metadata=artifact_metadata,
    )


def _inherits_parent_semantics(result: Any) -> bool:
    """同页补采继承父采集的页面类型，业务跳转仍等待重新识别。"""
    mode = str(getattr(result.mode, "value", result.mode) or "").strip().casefold()
    if mode in {"observe", "timed_capture"}:
        return True
    if mode != "single_trigger":
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("action") or "").strip().casefold() in {"scroll", "hover"}
        for item in list(getattr(result, "action_trace", []) or [])
    )


def _should_retry_without_surface_binding(
    *,
    error: RuntimeError,
    surface_session_id: str | None,
    action_execution_id: str | None,
    session_command_sequence: int | None,
    finalization_epoch: int | None,
    forbid_strip_binding: bool = False,
) -> bool:
    """运行态绑定失配时保留页面事实，父页面校验仍由仓储层执行。

    App 流程基建协议（``surface_session_id`` 以 ``app:`` 开头或显式
    ``forbid_strip_binding``）禁止剥绑定再入库（设计 §9.3）。
    """
    if forbid_strip_binding:
        return False
    if str(surface_session_id or "").startswith("app:"):
        return False
    if not any(
        value is not None
        for value in (
            surface_session_id,
            action_execution_id,
            session_command_sequence,
            finalization_epoch,
        )
    ):
        return False
    message = str(error)
    return any(
        marker in message
        for marker in (
            "Surface Session",
            "Action Execution",
            "Finalization Epoch",
            "命令序列",
        )
    )
