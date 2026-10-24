"""截图专用 App Capture 的校验、对象写入与数据库补偿编排。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from services.api.app.application.app_evidence_manifest import (
    AppEvidenceManifestFinalizer,
    build_pending_app_capture_manifest,
)
from services.api.app.application.app_metrics import (
    APP_CAPTURE_ORPHAN_TOTAL,
    record_app_metric_safely,
)
from services.api.app.application.capture_profiles import APP_CAPTURE_ARTIFACT_CONTRACT
from services.api.app.application.evidence_manifests import EvidenceManifestRepository
from services.api.app.application.page_capture_persistence import (
    EvidenceArtifactStore,
    PersistedEvidenceArtifact,
    _validate_stored_artifacts,
)
from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    RawArtifactSet,
    SurfaceKind,
)
from services.api.app.domain.interaction.validation import optional_text, require_text
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)

DEFAULT_MAX_APP_SCREENSHOT_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_APP_UI_TREE_BYTES = 256 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ALLOWED_METADATA_KEYS = frozenset(
    {
        "package_name",
        "activity",
        "width",
        "height",
        "orientation",
        "image_signature",
        "device_snapshot",
        "capture_artifact_contract",
        # 连拍审计（启动／开屏 temporal_trace）
        "timed_capture",
        # Toast／瞬时文案观察（P2-10）
        "toast_observation",
        # 外跳落地 URL（intent／层级／dumpsys 抽到的真实 https）；无则省略
        "outbound_landing",
        # 播放器采 media_state 前唤醒审计（非业务到达）
        "player_wake_before_capture",
        # 星语证据清单配套字段
        "session_auth",
        "is_obstructed",
        "requirement_role",
        "capture_role",
        "page_state",
        "page_type",
        "page_url",
        "outbound_target",
        "fullscreen",
        "exploration_action_id",
        "evidence_role",
        "network_trace",
        "har",
    }
)
_FORBIDDEN_DEVICE_KEYS = frozenset(
    {
        "dom",
        "html",
        "visible_text",
        "ui_tree",
        "ui_snapshot",
        "ocr_text",
        "locator",
    }
)
_LOGGER = logging.getLogger(__name__)


def build_app_action_capture_id(*, action_execution_id: str, role: str) -> str:
    """生成动作 Capture 的稳定业务身份，恢复路径必须复用同一条事实。"""

    require_text(action_execution_id, "action_execution_id")
    if role not in {"before_action", "after_action"}:
        raise ValueError("App Action Capture role 不受支持")
    return str(
        uuid5(
            NAMESPACE_URL,
            f"alioth:app-capture:{action_execution_id}:{role}",
        )
    )


@dataclass(frozen=True, kw_only=True)
class PersistedAppCapture:
    """已持久化的公共 Capture 引用与 App 截图事实。"""

    capture: CaptureRef
    agent_run_id: str
    screenshot: PersistedEvidenceArtifact
    width: int
    height: int
    orientation: str
    image_signature: str
    package_name: str | None
    activity: str | None
    bootstrap_row_version: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capture, CaptureRef):
            raise ValueError("capture 必须为 CaptureRef")
        require_text(self.agent_run_id, "agent_run_id")
        if (
            self.capture.capture_kind is not CaptureKind.APP
            or self.capture.surface_kind is SurfaceKind.WEB
        ):
            raise ValueError("PersistedAppCapture 必须引用 App Capture")
        if self.capture.role not in {
            "bootstrap",
            "before_action",
            "after_action",
            "observation",
        }:
            raise ValueError("PersistedAppCapture role 不受支持")
        if not isinstance(self.screenshot, PersistedEvidenceArtifact):
            raise ValueError("screenshot 类型不正确")
        if self.screenshot.artifact_type != "screenshot":
            raise ValueError("App Capture 只允许 Screenshot Artifact")
        if self.screenshot.artifact_id not in self.capture.artifact_ids:
            raise ValueError("CaptureRef 与 Screenshot Artifact 身份不一致")
        for field_name in ("width", "height"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} 必须为正整数")
        if self.orientation not in {"portrait", "landscape", "unknown"}:
            raise ValueError("orientation 不受支持")
        require_text(self.image_signature, "image_signature")
        if self.screenshot.checksum != self.image_signature:
            raise ValueError("Screenshot checksum 与 image_signature 不一致")
        optional_text(self.package_name, "package_name")
        optional_text(self.activity, "activity")
        if self.capture.role == "bootstrap":
            if (
                isinstance(self.bootstrap_row_version, bool)
                or not isinstance(self.bootstrap_row_version, int)
                or self.bootstrap_row_version < 0
            ):
                raise ValueError("Bootstrap Capture 必须携带非负检查点版本")
        elif self.capture.role == "observation":
            if (
                self.bootstrap_row_version is not None
                or self.capture.parent_capture_id is None
                or self.capture.action_execution_id is not None
            ):
                raise ValueError("Observation Capture 必须只绑定父 Capture")
        elif (
            self.bootstrap_row_version is not None
            or self.capture.parent_capture_id is None
            or self.capture.action_execution_id is None
        ):
            raise ValueError("动作 Capture 必须绑定父 Capture 和 Action Execution")

    @property
    def capture_id(self) -> str:
        return self.capture.capture_id


@dataclass(frozen=True, kw_only=True)
class VerifiedAppUiTree:
    """已通过 Capture 归属、大小与 SHA-256 校验的 UI Hierarchy。"""

    artifact: PersistedEvidenceArtifact
    xml_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, PersistedEvidenceArtifact):
            raise ValueError("artifact 必须为 PersistedEvidenceArtifact")
        if self.artifact.artifact_type != "ui_tree":
            raise ValueError("VerifiedAppUiTree 只接受 ui_tree")
        require_text(self.xml_text, "xml_text")


class AppCaptureRecoveryError(RuntimeError):
    """恢复所需 Capture 对象不可验证时的结构化、不可重试错误。"""

    retryable = False

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# 兼容 Batch 5A 早期调用方的名称；新代码统一使用 AppCaptureRecoveryError。
AppCaptureRecoveryReadError = AppCaptureRecoveryError


class AppCaptureRepository(Protocol):
    """App Capture 事实与 Bootstrap 绑定的 PostgreSQL 端口。"""

    async def get_bootstrap_capture(
        self,
        *,
        task_id: str,
        session_id: str,
    ) -> PersistedAppCapture | None: ...

    async def save_bootstrap_capture(self, **kwargs: Any) -> PersistedAppCapture: ...

    async def get_capture(self, *, capture_id: str) -> PersistedAppCapture | None: ...

    async def get_capture_artifact(
        self,
        *,
        capture_id: str,
        artifact_type: str,
    ) -> PersistedEvidenceArtifact | None: ...

    async def get_action_capture(
        self,
        *,
        action_execution_id: str,
        role: str,
    ) -> PersistedAppCapture | None: ...

    async def save_action_capture(self, **kwargs: Any) -> PersistedAppCapture: ...

    async def save_observation_capture(self, **kwargs: Any) -> PersistedAppCapture: ...


class AppCapturePersistenceService:
    """持久化 App 截图证据，并在数据库失败时补偿删除已写对象。"""

    def __init__(
        self,
        *,
        repository: AppCaptureRepository,
        artifact_store: EvidenceArtifactStore,
        manifest_repository: EvidenceManifestRepository | None = None,
        max_screenshot_bytes: int = DEFAULT_MAX_APP_SCREENSHOT_BYTES,
    ) -> None:
        if (
            isinstance(max_screenshot_bytes, bool)
            or not isinstance(max_screenshot_bytes, int)
            or max_screenshot_bytes <= 0
        ):
            raise ValueError("max_screenshot_bytes 必须大于 0")
        self._repository = repository
        self._artifact_store = artifact_store
        self._manifest_repository = manifest_repository
        self._max_screenshot_bytes = max_screenshot_bytes

    @property
    def _repository_persists_manifest_atomically(self) -> bool:
        return bool(getattr(self._repository, "persists_manifest_atomically", False))

    async def _settle_pending_manifest(
        self,
        manifest: EvidenceManifestReference,
        *,
        manifest_already_committed: bool,
        committed_task_id: str,
        committed_capture_id: str,
    ) -> None:
        """提交或校验 Manifest；生产 PG 路径不允许静默丢失。"""

        repository = self._manifest_repository
        if repository is None and self._repository_persists_manifest_atomically:
            repository = PostgresEvidenceManifestRepository()
        if repository is None:
            return
        committed = await repository.get_latest_for_capture(
            task_id=committed_task_id,
            capture_id=committed_capture_id,
            require_quality_pass=False,
        )
        if committed is None:
            if manifest_already_committed:
                raise RuntimeError("App Capture 已提交但同事务 PENDING Manifest 缺失")
            if (
                manifest.task_id != committed_task_id
                or manifest.capture_id != committed_capture_id
            ):
                raise RuntimeError("App Capture 重放后无法为历史 Artifact 补写 Manifest")
            await repository.add(manifest=manifest)
            committed = manifest
        elif (
            committed.task_id != committed_task_id
            or committed.capture_id != committed_capture_id
        ):
            raise RuntimeError("App Capture 与已提交 Manifest 归属不一致")
        if self._repository_persists_manifest_atomically:
            await AppEvidenceManifestFinalizer(
                repository=repository,
                artifact_store=self._artifact_store,
            ).finalize(committed)

    async def get_bootstrap_capture(
        self,
        *,
        task_id: str,
        session_id: str,
    ) -> PersistedAppCapture | None:
        """恢复时只读取已绑定检查点的 Capture，不触发设备重新截图。"""
        require_text(task_id, "task_id")
        require_text(session_id, "session_id")
        return await self._repository.get_bootstrap_capture(
            task_id=task_id,
            session_id=session_id,
        )

    async def get_capture(self, *, capture_id: str) -> PersistedAppCapture | None:
        """按公共 Capture ID 读取任意 App 截图事实。"""
        require_text(capture_id, "capture_id")
        return await self._repository.get_capture(capture_id=capture_id)

    async def get_verified_ui_tree(
        self,
        *,
        capture: CaptureRef,
    ) -> VerifiedAppUiTree | None:
        """读取当前 Capture 的 UI Hierarchy；缺失时不生成动作候选。"""

        if not isinstance(capture, CaptureRef):
            raise TypeError("capture 必须为 CaptureRef")
        persisted = await self.get_capture(capture_id=capture.capture_id)
        if persisted is None or persisted.capture != capture:
            return None
        reader = getattr(self._repository, "get_capture_artifact", None)
        if not callable(reader):
            return None
        artifact = await reader(
            capture_id=capture.capture_id,
            artifact_type="ui_tree",
        )
        if artifact is None:
            return None
        if (
            not isinstance(artifact, PersistedEvidenceArtifact)
            or artifact.artifact_type != "ui_tree"
            or artifact.artifact_id not in capture.artifact_ids
            or not artifact.content_type.startswith("application/xml")
            or artifact.size_bytes <= 0
            or artifact.size_bytes > DEFAULT_MAX_APP_UI_TREE_BYTES
        ):
            raise AppCaptureRecoveryError(
                "App UI Tree 元数据或归属无效",
                error_code="APP_UI_TREE_INVALID",
            )
        try:
            content = await self._artifact_store.read(artifact.storage_path)
        except Exception as exc:  # noqa: BLE001 - 统一为结构化的只读证据失败
            raise AppCaptureRecoveryError(
                "App UI Tree 对象不可读取",
                error_code="APP_UI_TREE_UNREADABLE",
            ) from exc
        if not isinstance(content, (bytes, bytearray)):
            raise AppCaptureRecoveryError(
                "App UI Tree 对象不是二进制内容",
                error_code="APP_UI_TREE_INVALID",
            )
        raw = bytes(content)
        checksum = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        if len(raw) != artifact.size_bytes or checksum != artifact.checksum:
            raise AppCaptureRecoveryError(
                "App UI Tree 对象与已持久化事实不一致",
                error_code="APP_UI_TREE_CHECKSUM_MISMATCH",
            )
        try:
            xml_text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise AppCaptureRecoveryError(
                "App UI Tree 不是 UTF-8 XML",
                error_code="APP_UI_TREE_INVALID",
            ) from exc
        if "<hierarchy" not in xml_text:
            raise AppCaptureRecoveryError(
                "App UI Tree 不包含 hierarchy 根节点",
                error_code="APP_UI_TREE_INVALID",
            )
        return VerifiedAppUiTree(artifact=artifact, xml_text=xml_text)

    async def get_action_capture(
        self,
        *,
        action_execution_id: str,
        role: str,
    ) -> PersistedAppCapture | None:
        """按动作业务身份读取历史 Capture，兼容旧的随机 Capture UUID。"""

        require_text(action_execution_id, "action_execution_id")
        _validate_action_capture_role(role)
        reader = getattr(self._repository, "get_action_capture", None)
        if not callable(reader):
            raise AppCaptureRecoveryError(
                "App Capture 仓储不支持按动作身份恢复",
                error_code="APP_ACTION_RECOVERY_CAPTURE_READER_MISSING",
            )
        persisted = await reader(
            action_execution_id=action_execution_id,
            role=role,
        )
        if persisted is None:
            return None
        _validate_action_capture_identity(
            persisted,
            action_execution_id=action_execution_id,
            role=role,
        )
        return persisted

    async def get_verified_capture(
        self,
        *,
        capture_id: str,
    ) -> PersistedAppCapture | None:
        """读取并验证截图对象；对象缺失、篡改或 PNG 无效均 fail-closed。"""

        persisted = await self.get_capture(capture_id=capture_id)
        if persisted is None:
            return None
        return await self._verify_screenshot_object(persisted)

    async def get_verified_action_capture(
        self,
        *,
        action_execution_id: str,
        role: str,
    ) -> PersistedAppCapture | None:
        """按动作身份读取并验证历史截图，不触发设备截图或生成新事实。"""

        persisted = await self.get_action_capture(
            action_execution_id=action_execution_id,
            role=role,
        )
        if persisted is None:
            return None
        return await self._verify_screenshot_object(persisted)

    async def _verify_screenshot_object(
        self,
        persisted: PersistedAppCapture,
    ) -> PersistedAppCapture:
        screenshot = persisted.screenshot
        try:
            content = await self._artifact_store.read(screenshot.storage_path)
        except AppCaptureRecoveryError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一为结构化恢复失败
            raise AppCaptureRecoveryError(
                "App Screenshot 对象不可读取",
                error_code="APP_ACTION_RECOVERY_CAPTURE_UNREADABLE",
            ) from exc
        if not isinstance(content, (bytes, bytearray)):
            raise AppCaptureRecoveryError(
                "App Screenshot 对象不是二进制内容",
                error_code="APP_ACTION_RECOVERY_CAPTURE_INVALID",
            )
        raw = bytes(content)
        if len(raw) > self._max_screenshot_bytes:
            raise AppCaptureRecoveryError(
                "App Screenshot 对象超过恢复读取上限",
                error_code="APP_ACTION_RECOVERY_CAPTURE_INVALID",
            )
        if len(raw) != screenshot.size_bytes:
            raise AppCaptureRecoveryError(
                "App Screenshot 对象大小与已持久化事实不一致",
                error_code="APP_ACTION_RECOVERY_CAPTURE_CHECKSUM_MISMATCH",
            )
        checksum = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        if checksum != screenshot.checksum or checksum != persisted.image_signature:
            raise AppCaptureRecoveryError(
                "App Screenshot 对象 SHA-256 与已持久化事实不一致",
                error_code="APP_ACTION_RECOVERY_CAPTURE_CHECKSUM_MISMATCH",
            )
        try:
            width, height = _read_png_dimensions(raw)
        except (TypeError, ValueError) as exc:
            raise AppCaptureRecoveryError(
                "App Screenshot 对象不是合法 PNG",
                error_code="APP_ACTION_RECOVERY_CAPTURE_INVALID",
            ) from exc
        if (
            screenshot.artifact_type != "screenshot"
            or screenshot.content_type != "image/png"
            or width != persisted.width
            or height != persisted.height
        ):
            raise AppCaptureRecoveryError(
                "App Screenshot 对象的类型或尺寸与已持久化事实不一致",
                error_code="APP_ACTION_RECOVERY_CAPTURE_INVALID",
            )
        return persisted

    async def persist_bootstrap(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        artifacts: RawArtifactSet,
        expected_bootstrap_row_version: int,
        workflow_lease_owner: str,
        workflow_lease_token: str,
    ) -> PersistedAppCapture:
        for field_name, value in (
            ("task_id", task_id),
            ("agent_run_id", agent_run_id),
            ("session_id", session_id),
            ("workflow_lease_owner", workflow_lease_owner),
            ("workflow_lease_token", workflow_lease_token),
        ):
            require_text(value, field_name)
        if (
            isinstance(expected_bootstrap_row_version, bool)
            or not isinstance(expected_bootstrap_row_version, int)
            or expected_bootstrap_row_version < 0
        ):
            raise ValueError("expected_bootstrap_row_version 必须为非负整数")
        if not isinstance(artifacts, RawArtifactSet):
            raise TypeError("artifacts 必须为 RawArtifactSet")
        if artifacts.task_id != task_id or artifacts.session_id != session_id:
            raise ValueError("RawArtifactSet 与任务或 Session 身份不一致")

        if len(artifacts.artifacts) != 1 or artifacts.artifacts[0].artifact_type != "screenshot":
            raise ValueError("App Bootstrap 且仅允许一个 screenshot")
        screenshot_bytes = artifacts.artifacts[0].content
        if not isinstance(screenshot_bytes, bytes):
            raise ValueError("App Screenshot 必须为二进制 PNG")
        if len(screenshot_bytes) > self._max_screenshot_bytes:
            raise ValueError("App Screenshot 超过上限")
        width, height = _read_png_dimensions(screenshot_bytes)
        metadata = _validate_metadata(artifacts.metadata)
        _validate_reported_dimensions(metadata, width=width, height=height)
        image_signature = f"sha256:{hashlib.sha256(screenshot_bytes).hexdigest()}"
        reported_signature = metadata.get("image_signature")
        if reported_signature is not None and reported_signature != image_signature:
            raise ValueError("image_signature 与 Screenshot 内容不一致")
        orientation = str(metadata.get("orientation") or "unknown")
        if orientation not in {"portrait", "landscape", "unknown"}:
            raise ValueError("orientation 不受支持")
        package_name = _optional_metadata_text(metadata, "package_name")
        activity = _optional_metadata_text(metadata, "activity")
        device_snapshot = _validate_device_snapshot(metadata.get("device_snapshot", {}))
        existing = await self._repository.get_bootstrap_capture(
            task_id=task_id,
            session_id=session_id,
        )
        if existing is not None:
            if existing.image_signature != image_signature:
                raise ValueError("App Bootstrap 已绑定不同的 Screenshot 内容")
            pending_manifest = build_pending_app_capture_manifest(
                task_id=existing.capture.task_id,
                session_id=existing.capture.session_id,
                capture_id=existing.capture.capture_id,
                capture_role=existing.capture.role,
                artifacts=(existing.screenshot,),
            )
            await self._settle_pending_manifest(
                pending_manifest,
                manifest_already_committed=False,
                committed_task_id=existing.capture.task_id,
                committed_capture_id=existing.capture.capture_id,
            )
            return existing

        capture_id = str(uuid4())
        artifact_id = str(uuid4())
        stored: PersistedEvidenceArtifact | None = None
        capture_committed = False
        try:
            stored = await self._artifact_store.store(
                task_id=task_id,
                page_capture_id=capture_id,
                artifact_id=artifact_id,
                artifact_type="screenshot",
                content=screenshot_bytes,
            )
            stored = _with_screenshot_metadata(
                stored,
                width=width,
                height=height,
                orientation=orientation,
                image_signature=image_signature,
                session_auth=metadata["session_auth"],
            )
            _validate_stored_screenshot(
                stored,
                expected_artifact_id=artifact_id,
                expected_signature=image_signature,
                expected_size=len(screenshot_bytes),
            )
            pending_manifest = build_pending_app_capture_manifest(
                task_id=task_id,
                session_id=session_id,
                capture_id=capture_id,
                capture_role="bootstrap",
                artifacts=(stored,),
            )
            persisted = await self._repository.save_bootstrap_capture(
                task_id=task_id,
                agent_run_id=agent_run_id,
                session_id=session_id,
                capture_id=capture_id,
                screenshot=stored,
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
                manifest=pending_manifest,
            )
            capture_committed = True
            await self._settle_pending_manifest(
                pending_manifest,
                manifest_already_committed=self._repository_persists_manifest_atomically,
                committed_task_id=persisted.capture.task_id,
                committed_capture_id=persisted.capture.capture_id,
            )
            return persisted
        except BaseException:
            if stored is not None and not capture_committed:
                try:
                    if stored.created:
                        await asyncio.wait_for(
                            self._artifact_store.delete(stored.storage_path), timeout=5
                        )
                except BaseException:  # noqa: BLE001  # 补偿失败不得遮蔽原异常
                    record_app_metric_safely(
                        APP_CAPTURE_ORPHAN_TOTAL,
                    )
                    _LOGGER.warning(
                        "app_capture_object_compensation_failed",
                        extra={
                            "task_id": task_id,
                            "capture_id": capture_id,
                            "artifact_id": stored.artifact_id,
                            "storage_path": stored.storage_path,
                        },
                    )
            raise

    async def persist_action_capture(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        surface_kind: SurfaceKind,
        artifacts: RawArtifactSet,
        role: str,
        required_artifact_types: tuple[str, ...],
        parent_capture_id: str,
        action_execution_id: str,
        session_command_sequence: int,
        finalization_epoch: int,
        lease_owner: str,
        lease_token: str,
    ) -> PersistedAppCapture:
        """提交动作前后截图，并把审计引用绑定到当前 Session 命令与 Lease。"""
        for field_name, value in (
            ("task_id", task_id),
            ("agent_run_id", agent_run_id),
            ("session_id", session_id),
            ("parent_capture_id", parent_capture_id),
            ("action_execution_id", action_execution_id),
            ("lease_owner", lease_owner),
            ("lease_token", lease_token),
        ):
            require_text(value, field_name)
        if not isinstance(surface_kind, SurfaceKind) or surface_kind is SurfaceKind.WEB:
            raise ValueError("App Action Capture 必须属于 App Surface")
        if role not in {"before_action", "after_action"}:
            raise ValueError("App Action Capture role 不受支持")
        for field_name, value in (
            ("session_command_sequence", session_command_sequence),
            ("finalization_epoch", finalization_epoch),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须为非负整数")
        if session_command_sequence == 0:
            raise ValueError("session_command_sequence 必须为正整数")
        if not isinstance(artifacts, RawArtifactSet):
            raise TypeError("artifacts 必须为 RawArtifactSet")
        if artifacts.task_id != task_id or artifacts.session_id != session_id:
            raise ValueError("RawArtifactSet 与任务或 Session 身份不一致")
        required = _validate_persisted_capture_types(
            required_artifact_types,
            role=role,
        )
        raw_by_type = {item.artifact_type: item for item in artifacts.artifacts}
        if len(raw_by_type) != len(artifacts.artifacts) or set(raw_by_type) != set(
            required
        ):
            raise ValueError("App Action Capture 产物与 Capture Profile 不一致")
        screenshot_bytes = raw_by_type["screenshot"].content
        if not isinstance(screenshot_bytes, bytes):
            raise ValueError("App Screenshot 必须为二进制 PNG")
        if len(screenshot_bytes) > self._max_screenshot_bytes:
            raise ValueError("App Screenshot 超过上限")
        width, height = _read_png_dimensions(screenshot_bytes)
        metadata = _validate_metadata(artifacts.metadata)
        _validate_capture_artifact_contract_consistency(
            metadata.get("capture_artifact_contract"),
            role=role,
            persisted_artifact_types=required,
        )
        _validate_reported_dimensions(metadata, width=width, height=height)
        image_signature = f"sha256:{hashlib.sha256(screenshot_bytes).hexdigest()}"
        if metadata.get("image_signature") not in {None, image_signature}:
            raise ValueError("image_signature 与 Screenshot 内容不一致")
        orientation = str(metadata.get("orientation") or "unknown")
        if orientation not in {"portrait", "landscape", "unknown"}:
            raise ValueError("orientation 不受支持")
        package_name = _optional_metadata_text(metadata, "package_name")
        activity = _optional_metadata_text(metadata, "activity")
        device_snapshot = _validate_device_snapshot(metadata.get("device_snapshot", {}))
        if "action_trace" in raw_by_type:
            forbidden = _find_forbidden_device_keys(raw_by_type["action_trace"].content)
            if forbidden:
                raise ValueError("action_trace 禁止携带页面正文：" + ", ".join(sorted(forbidden)))
        if "ui_tree" in required:
            ui_tree = raw_by_type["ui_tree"].content
            if isinstance(ui_tree, (bytes, bytearray)):
                ui_text = bytes(ui_tree).decode("utf-8", errors="replace")
            elif isinstance(ui_tree, str):
                ui_text = ui_tree
            else:
                raise ValueError("ui_tree 必须为 XML 文本或 UTF-8 字节")
            if "<hierarchy" not in ui_text and not ui_text.lstrip().startswith("<?xml"):
                raise ValueError("ui_tree 必须为 uiautomator hierarchy XML")
        selected_raw = tuple(raw_by_type[item] for item in required)

        # Action Capture 和 Artifact 身份由业务幂等键决定，重试不能产生第二条事实。
        # Capture 命名空间模板：alioth:app-capture:{action_execution_id}:{role}
        capture_id = build_app_action_capture_id(
            action_execution_id=action_execution_id,
            role=role,
        )
        stored_artifacts: list[PersistedEvidenceArtifact] = []
        capture_committed = False
        try:
            for raw in selected_raw:
                artifact_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        f"alioth:app-artifact:{action_execution_id}:{role}:{raw.artifact_type}",
                    )
                )
                stored = await self._artifact_store.store(
                    task_id=task_id,
                    page_capture_id=capture_id,
                    artifact_id=artifact_id,
                    artifact_type=raw.artifact_type,
                    content=raw.content,
                )
                if not isinstance(stored, PersistedEvidenceArtifact):
                    raise ValueError("Evidence Store 未返回可补偿的 Artifact 引用")
                # 当前调用生成的是新的 artifact_id；先纳入补偿列表，再校验元数据，
                # 使校验失败时也能删除刚上传的对象。
                stored_artifacts.append(stored)
                _validate_stored_artifact(
                    stored,
                    expected_artifact_id=artifact_id,
                    expected_type=raw.artifact_type,
                )
                if raw.artifact_type == "screenshot":
                    stored = _with_screenshot_metadata(
                        stored,
                        width=width,
                        height=height,
                        orientation=orientation,
                        image_signature=image_signature,
                        session_auth=metadata["session_auth"],
                        capture_artifact_contract=metadata.get("capture_artifact_contract"),
                    )
                    stored_artifacts[-1] = stored
            screenshot = next(
                item for item in stored_artifacts if item.artifact_type == "screenshot"
            )
            _validate_stored_screenshot(
                screenshot,
                expected_artifact_id=screenshot.artifact_id,
                expected_signature=image_signature,
                expected_size=len(screenshot_bytes),
            )
            pending_manifest = build_pending_app_capture_manifest(
                task_id=task_id,
                session_id=session_id,
                capture_id=capture_id,
                capture_role=role,
                artifacts=tuple(stored_artifacts),
            )
            persisted = await self._repository.save_action_capture(
                task_id=task_id,
                agent_run_id=agent_run_id,
                session_id=session_id,
                surface_kind=surface_kind,
                capture_id=capture_id,
                artifacts=tuple(stored_artifacts),
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
            )
            capture_committed = True
            referenced_artifact_ids = set(persisted.capture.artifact_ids)
            for stored in stored_artifacts:
                if not stored.created or stored.artifact_id in referenced_artifact_ids:
                    continue
                try:
                    await asyncio.wait_for(
                        self._artifact_store.delete(stored.storage_path), timeout=5
                    )
                except BaseException:  # noqa: BLE001 - 成功持久化后清理失败不得回滚结果
                    record_app_metric_safely(
                        APP_CAPTURE_ORPHAN_TOTAL,
                    )
                    _LOGGER.warning(
                        "app_capture_unreferenced_object_cleanup_failed",
                        extra={
                            "task_id": task_id,
                            "capture_id": capture_id,
                            "artifact_id": stored.artifact_id,
                            "storage_path": stored.storage_path,
                        },
                    )
            await self._settle_pending_manifest(
                pending_manifest,
                manifest_already_committed=self._repository_persists_manifest_atomically,
                committed_task_id=persisted.capture.task_id,
                committed_capture_id=persisted.capture.capture_id,
            )
            return persisted
        except BaseException:
            for stored in reversed(stored_artifacts if not capture_committed else []):
                try:
                    if stored.created:
                        await asyncio.wait_for(self._artifact_store.delete(stored.storage_path), 5)
                except BaseException:  # noqa: BLE001 - 补偿失败不得遮蔽原异常
                    record_app_metric_safely(
                        APP_CAPTURE_ORPHAN_TOTAL,
                    )
                    _LOGGER.warning(
                        "app_capture_object_compensation_failed",
                        extra={
                            "task_id": task_id,
                            "capture_id": capture_id,
                            "artifact_id": stored.artifact_id,
                            "storage_path": stored.storage_path,
                        },
                    )
            raise

    async def persist_observation_capture(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        surface_kind: SurfaceKind,
        artifacts: RawArtifactSet,
        required_artifact_types: tuple[str, ...],
        parent_capture_id: str,
        observation_id: str,
        surface_phase_run_id: str,
        surface_phase_decision_id: str,
        semantic_role: str = "surface_reobserve",
        session_command_sequence: int,
        finalization_epoch: int,
        lease_owner: str,
        lease_token: str,
    ) -> PersistedAppCapture:
        """提交不绑定 Action Execution 的 Surface 重新观察证据。"""
        for field_name, value in (
            ("task_id", task_id),
            ("agent_run_id", agent_run_id),
            ("session_id", session_id),
            ("parent_capture_id", parent_capture_id),
            ("observation_id", observation_id),
            ("surface_phase_run_id", surface_phase_run_id),
            ("surface_phase_decision_id", surface_phase_decision_id),
            ("semantic_role", semantic_role),
            ("lease_owner", lease_owner),
            ("lease_token", lease_token),
        ):
            require_text(value, field_name)
        if semantic_role not in {"surface_reobserve", "final_observation"}:
            raise ValueError("Observation semantic_role 不受支持")
        if not isinstance(surface_kind, SurfaceKind) or surface_kind is SurfaceKind.WEB:
            raise ValueError("App Observation Capture 必须属于 App Surface")
        for field_name, value in (
            ("session_command_sequence", session_command_sequence),
            ("finalization_epoch", finalization_epoch),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须为非负整数")
        if session_command_sequence == 0:
            raise ValueError("session_command_sequence 必须为正整数")
        if not isinstance(artifacts, RawArtifactSet):
            raise TypeError("artifacts 必须为 RawArtifactSet")
        if artifacts.task_id != task_id or artifacts.session_id != session_id:
            raise ValueError("RawArtifactSet 与任务或 Session 身份不一致")
        required = _validate_persisted_capture_types(
            required_artifact_types,
            role="observation",
        )
        all_raw_by_type = {item.artifact_type: item for item in artifacts.artifacts}
        if len(all_raw_by_type) != len(artifacts.artifacts) or set(all_raw_by_type) != set(
            required
        ):
            raise ValueError("App Observation Capture 产物与 Capture Directive 不一致")
        selected_raw = tuple(all_raw_by_type[item] for item in required)
        screenshot_bytes = all_raw_by_type["screenshot"].content
        if not isinstance(screenshot_bytes, bytes):
            raise ValueError("App Screenshot 必须为二进制 PNG")
        if len(screenshot_bytes) > self._max_screenshot_bytes:
            raise ValueError("App Screenshot 超过上限")
        width, height = _read_png_dimensions(screenshot_bytes)
        metadata = _validate_metadata(artifacts.metadata)
        _validate_capture_artifact_contract_consistency(
            metadata.get("capture_artifact_contract"),
            role="observation",
            persisted_artifact_types=required,
        )
        _validate_reported_dimensions(metadata, width=width, height=height)
        image_signature = f"sha256:{hashlib.sha256(screenshot_bytes).hexdigest()}"
        if metadata.get("image_signature") not in {None, image_signature}:
            raise ValueError("image_signature 与 Screenshot 内容不一致")
        orientation = str(metadata.get("orientation") or "unknown")
        if orientation not in {"portrait", "landscape", "unknown"}:
            raise ValueError("orientation 不受支持")
        package_name = _optional_metadata_text(metadata, "package_name")
        activity = _optional_metadata_text(metadata, "activity")
        device_snapshot = _validate_device_snapshot(metadata.get("device_snapshot", {}))
        ui_tree_raw = all_raw_by_type.get("ui_tree")
        if ui_tree_raw is not None and "ui_tree" in required:
            ui_tree = ui_tree_raw.content
            if isinstance(ui_tree, (bytes, bytearray)):
                ui_text = bytes(ui_tree).decode("utf-8", errors="replace")
            elif isinstance(ui_tree, str):
                ui_text = ui_tree
            else:
                raise ValueError("ui_tree 必须为 XML 文本或 UTF-8 字节")
            if "<hierarchy" not in ui_text and not ui_text.lstrip().startswith("<?xml"):
                raise ValueError("ui_tree 必须为 uiautomator hierarchy XML")

        capture_id = str(uuid5(NAMESPACE_URL, f"alioth:app-observation:{observation_id}"))
        stored_artifacts: list[PersistedEvidenceArtifact] = []
        capture_committed = False
        try:
            for raw in selected_raw:
                artifact_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        f"alioth:app-observation-artifact:{observation_id}:{raw.artifact_type}",
                    )
                )
                stored = await self._artifact_store.store(
                    task_id=task_id,
                    page_capture_id=capture_id,
                    artifact_id=artifact_id,
                    artifact_type=raw.artifact_type,
                    content=raw.content,
                )
                if not isinstance(stored, PersistedEvidenceArtifact):
                    raise ValueError("Evidence Store 未返回可补偿的 Artifact 引用")
                stored_artifacts.append(stored)
                _validate_stored_artifact(
                    stored,
                    expected_artifact_id=artifact_id,
                    expected_type=raw.artifact_type,
                )
                if raw.artifact_type == "screenshot":
                    stored_artifacts[-1] = _with_screenshot_metadata(
                        stored,
                        width=width,
                        height=height,
                        orientation=orientation,
                        image_signature=image_signature,
                        session_auth=metadata["session_auth"],
                        capture_artifact_contract=metadata.get("capture_artifact_contract"),
                    )
            screenshot = next(
                item for item in stored_artifacts if item.artifact_type == "screenshot"
            )
            pending_manifest = build_pending_app_capture_manifest(
                task_id=task_id,
                session_id=session_id,
                capture_id=capture_id,
                capture_role="observation",
                artifacts=tuple(stored_artifacts),
            )
            persisted = await self._repository.save_observation_capture(
                task_id=task_id,
                agent_run_id=agent_run_id,
                session_id=session_id,
                surface_kind=surface_kind,
                capture_id=capture_id,
                artifacts=tuple(stored_artifacts),
                screenshot=screenshot,
                width=width,
                height=height,
                orientation=orientation,
                image_signature=image_signature,
                package_name=package_name,
                activity=activity,
                device_snapshot=device_snapshot,
                role="observation",
                parent_capture_id=parent_capture_id,
                observation_id=observation_id,
                surface_phase_run_id=surface_phase_run_id,
                surface_phase_decision_id=surface_phase_decision_id,
                semantic_role=semantic_role,
                session_command_sequence=session_command_sequence,
                finalization_epoch=finalization_epoch,
                lease_owner=lease_owner,
                lease_token=lease_token,
            )
            capture_committed = True
            referenced = set(persisted.capture.artifact_ids)
            for stored in stored_artifacts:
                if stored.created and stored.artifact_id not in referenced:
                    try:
                        await asyncio.wait_for(
                            self._artifact_store.delete(stored.storage_path), timeout=5
                        )
                    except BaseException:  # noqa: BLE001 - 清理失败不回滚已提交事实
                        _LOGGER.warning(
                            "app_observation_unreferenced_object_cleanup_failed",
                            extra={"task_id": task_id, "capture_id": capture_id},
                        )
            await self._settle_pending_manifest(
                pending_manifest,
                manifest_already_committed=self._repository_persists_manifest_atomically,
                committed_task_id=persisted.capture.task_id,
                committed_capture_id=persisted.capture.capture_id,
            )
            return persisted
        except BaseException:
            for stored in reversed(stored_artifacts if not capture_committed else []):
                if not stored.created:
                    continue
                try:
                    await asyncio.wait_for(
                        self._artifact_store.delete(stored.storage_path), timeout=5
                    )
                except BaseException:  # noqa: BLE001 - 保留原始异常
                    _LOGGER.warning(
                        "app_observation_object_compensation_failed",
                        extra={"task_id": task_id, "capture_id": capture_id},
                    )
            raise


def _read_png_dimensions(content: bytes) -> tuple[int, int]:
    if len(content) < 33 or not content.startswith(_PNG_SIGNATURE):
        raise ValueError("App Screenshot PNG signature 不合法")
    if int.from_bytes(content[8:12], "big") != 13 or content[12:16] != b"IHDR":
        raise ValueError("App Screenshot 缺少合法 IHDR")
    width = int.from_bytes(content[16:20], "big")
    height = int.from_bytes(content[20:24], "big")
    if width <= 0 or height <= 0:
        raise ValueError("App Screenshot PNG 尺寸必须大于 0")
    return width, height


def _validate_action_capture_role(role: str) -> None:
    if role not in {"before_action", "after_action"}:
        raise ValueError("App Action Capture role 不受支持")


def _validate_persisted_capture_types(
    artifact_types: tuple[str, ...],
    *,
    role: str,
) -> tuple[str, ...]:
    """按统一合同校验 Persistence 实际接收的制品集合。"""

    values = tuple(artifact_types)
    if len(set(values)) != len(values):
        raise ValueError("App Capture Artifact 类型不得重复")
    if not values:
        raise ValueError("App Capture Artifact 类型不能为空")
    required = APP_CAPTURE_ARTIFACT_CONTRACT.required_for(role)
    if not set(required).issubset(values):
        raise ValueError(
            "App Capture Persistence 缺少 required 制品："
            + ", ".join(sorted(set(required) - set(values)))
        )
    unsupported = set(values) - set(APP_CAPTURE_ARTIFACT_CONTRACT.persistable_artifact_types)
    if unsupported:
        raise ValueError(
            "App Capture Persistence 不允许未落库制品："
            + ", ".join(sorted(unsupported))
        )
    return values


def _validate_action_capture_identity(
    persisted: PersistedAppCapture,
    *,
    action_execution_id: str,
    role: str,
) -> None:
    if (
        not isinstance(persisted, PersistedAppCapture)
        or persisted.capture.action_execution_id != action_execution_id
        or persisted.capture.role != role
        or persisted.capture.surface_kind is SurfaceKind.WEB
    ):
        raise AppCaptureRecoveryError(
            "App 动作 Capture 的业务身份与查询条件不一致",
            error_code="APP_ACTION_RECOVERY_CAPTURE_INVALID",
        )


def _validate_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    from services.api.app.application.app_capture_evidence_fields import (
        enrich_capture_evidence_fields,
    )

    # 先补齐 session_auth／is_obstructed／requirement_role，再做白名单校验。
    enriched = enrich_capture_evidence_fields(metadata)
    unknown = sorted(set(enriched) - _ALLOWED_METADATA_KEYS)
    if unknown:
        raise ValueError("App Screenshot metadata 包含未授权字段：" + ", ".join(unknown))
    if "capture_artifact_contract" in enriched:
        _validate_capture_artifact_contract_metadata(enriched["capture_artifact_contract"])
    return enriched


def _validate_capture_artifact_contract_metadata(value: Any) -> None:
    """校验 Writer 写入的 required/soft-optional 状态摘要。"""

    if not isinstance(value, Mapping):
        raise ValueError("capture_artifact_contract 必须为对象")
    if value.get("contract_version") != "app-capture-artifact-contract.v3":
        raise ValueError("capture_artifact_contract 版本不受支持")
    if value.get("role") not in {"before_action", "after_action", "observation"}:
        raise ValueError("capture_artifact_contract role 不受支持")
    for field_name in (
        "requested_artifact_types",
        "produced_artifact_types",
        "persisted_artifact_types",
        "required_artifact_types",
        "missing_soft_optional_artifact_types",
        "provider_only_artifact_types",
    ):
        values = value.get(field_name)
        if not isinstance(values, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() for item in values
        ):
            raise ValueError(f"capture_artifact_contract.{field_name} 不合法")
        if len(set(values)) != len(values):
            raise ValueError(f"capture_artifact_contract.{field_name} 不得重复")


def _validate_capture_artifact_contract_consistency(
    value: Any,
    *,
    role: str,
    persisted_artifact_types: tuple[str, ...],
) -> None:
    """防止 Writer 状态摘要与实际进入 Persistence 的制品集合漂移。"""

    if value is None:
        return
    if value.get("role") != role:
        raise ValueError("capture_artifact_contract role 与 Capture role 不一致")
    selection = APP_CAPTURE_ARTIFACT_CONTRACT.select(
        role=role,
        requested=tuple(value.get("requested_artifact_types") or ()),
        produced=tuple(value.get("produced_artifact_types") or ()),
    )
    expected = selection.to_metadata()
    for field_name in (
        "persisted_artifact_types",
        "required_artifact_types",
        "missing_soft_optional_artifact_types",
        "provider_only_artifact_types",
    ):
        if tuple(value.get(field_name) or ()) != tuple(expected[field_name]):
            raise ValueError(
                f"capture_artifact_contract.{field_name} 与正式合同不一致"
            )
    if selection.persisted_artifact_types != persisted_artifact_types:
        raise ValueError(
            "capture_artifact_contract persisted 制品与实际写入集合不一致"
        )


def _validate_reported_dimensions(
    metadata: Mapping[str, Any],
    *,
    width: int,
    height: int,
) -> None:
    for field_name, actual in (("width", width), ("height", height)):
        reported = metadata.get(field_name)
        if reported is not None and reported != actual:
            raise ValueError(f"{field_name} 与 PNG 实际尺寸不一致")


def _optional_metadata_text(metadata: Mapping[str, Any], field_name: str) -> str | None:
    value = metadata.get(field_name)
    optional_text(value, field_name)
    return value


def _validate_device_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("device_snapshot 必须为对象")
    forbidden = sorted(_find_forbidden_device_keys(value))
    if forbidden:
        raise ValueError("device_snapshot 禁止携带页面正文：" + ", ".join(forbidden))
    return dict(value)


def _find_forbidden_device_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        found = {str(key) for key in value if str(key).casefold() in _FORBIDDEN_DEVICE_KEYS}
        for nested in value.values():
            found.update(_find_forbidden_device_keys(nested))
        return found
    if isinstance(value, (list, tuple)):
        found: set[str] = set()
        for nested in value:
            found.update(_find_forbidden_device_keys(nested))
        return found
    return set()


def _validate_stored_screenshot(
    artifact: PersistedEvidenceArtifact,
    *,
    expected_artifact_id: str,
    expected_signature: str,
    expected_size: int,
) -> None:
    _validate_stored_artifacts((artifact,))
    if (
        artifact.artifact_id != expected_artifact_id
        or artifact.artifact_type != "screenshot"
        or artifact.content_type != "image/png"
        or artifact.checksum != expected_signature
        or artifact.size_bytes != expected_size
    ):
        raise ValueError("Evidence Store 返回的 Screenshot 元数据与原始 PNG 不一致")


def _with_screenshot_metadata(
    artifact: PersistedEvidenceArtifact,
    *,
    width: int,
    height: int,
    orientation: str,
    image_signature: str,
    session_auth: object = "unknown",
    capture_artifact_contract: Mapping[str, Any] | None = None,
) -> PersistedEvidenceArtifact:
    """固化经 PNG 二进制校验的截图事实，避免数据库依赖调用方声明。"""

    from services.api.app.application.app_capture_evidence_fields import normalize_session_auth

    metadata = {
        **artifact.metadata,
        "width": width,
        "height": height,
        "orientation": orientation,
        "image_signature": image_signature,
        "session_auth": normalize_session_auth(session_auth),
    }
    if capture_artifact_contract is not None:
        metadata["capture_artifact_contract"] = dict(capture_artifact_contract)
    return PersistedEvidenceArtifact(
        artifact_id=artifact.artifact_id,
        artifact_type=artifact.artifact_type,
        storage_path=artifact.storage_path,
        content_type=artifact.content_type,
        checksum=artifact.checksum,
        size_bytes=artifact.size_bytes,
        metadata=metadata,
        created=artifact.created,
    )


def _validate_stored_artifact(
    artifact: PersistedEvidenceArtifact,
    *,
    expected_artifact_id: str,
    expected_type: str,
) -> None:
    _validate_stored_artifacts((artifact,))
    if (
        not isinstance(artifact, PersistedEvidenceArtifact)
        or artifact.artifact_id != expected_artifact_id
        or artifact.artifact_type != expected_type
    ):
        raise ValueError("Evidence Store 返回的 Artifact 元数据不一致")
