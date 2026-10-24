"""跨端六项流程基建：P0／P1 领域合同（设计 2026-10-22）。

本模块只定义不可变身份、三项分立结果、App 能力准入、采集归属守卫与
设备执行权裁决；不连接数据库、浏览器或 ADB。

P2 控制见 ``flow_foundation_control``；P3 连续证据见 ``flow_foundation_evidence``；
P4 试点门禁见 ``flow_foundation_pilot``；应用接线见 ``application.flow_foundation_control``。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from services.api.app.domain.interaction.capabilities import CapabilityStatus
from services.api.app.domain.interaction.enums import SurfaceKind
from services.api.app.domain.protocol import (
    ProtocolContractError,
    require_text,
    stable_protocol_hash,
)

FLOW_FOUNDATION_CONTRACT_VERSION = "flow-foundation.p0p1.v1"
FLOW_FOUNDATION_PROTOCOL_VERSION = "app-flow-foundation.v1"

# ---------------------------------------------------------------------------
# P0.6 身份词典（设计 §4.2）
# ---------------------------------------------------------------------------

FLOW_FOUNDATION_IDENTITY_KEYS = (
    "task_id",
    "target_platform",
    "surface_kind",
    "exploration_scope_id",
    "phase_run_id",
    "execution_segment_id",
    "subject_ref",
    "occurrence_id",
    "page_surface_epoch",
    "producer_epoch",
    "sample_index",
    "resource_event_seq",
    "resource_id",
    "device_id",
    "fencing_token",
    "session_id",
    "capture_id",
    "capture_request_id",
)


class FlowFoundationContractError(ProtocolContractError):
    """流程基建合同不满足。"""


class TargetPlatform(StrEnum):
    """业务端类型；与 SurfaceKind 执行界面分开。"""

    WEB = "web"
    H5 = "h5"
    APP = "app"


# ---------------------------------------------------------------------------
# P0.5 三项分立结果（设计 §4.3）
# ---------------------------------------------------------------------------


class FlowExecutionResult(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class FlowEvidenceState(StrEnum):
    PENDING = "pending"
    COLLECTING = "collecting"
    DRAINING = "draining"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    QUARANTINED = "quarantined"


class FlowResumeResult(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    RESTORED = "restored"
    REPLANNED = "replanned"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, kw_only=True)
class FlowTriStateOutcome:
    """流程执行／证据／恢复三项独立结果；禁止单 success 覆盖。"""

    execution_result: FlowExecutionResult
    evidence_state: FlowEvidenceState
    resume_result: FlowResumeResult
    protocol_version: str = FLOW_FOUNDATION_PROTOCOL_VERSION
    contract_version: str = FLOW_FOUNDATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.execution_result, FlowExecutionResult):
            raise FlowFoundationContractError("execution_result 无效")
        if not isinstance(self.evidence_state, FlowEvidenceState):
            raise FlowFoundationContractError("evidence_state 无效")
        if not isinstance(self.resume_result, FlowResumeResult):
            raise FlowFoundationContractError("resume_result 无效")
        if self.protocol_version != FLOW_FOUNDATION_PROTOCOL_VERSION:
            raise FlowFoundationContractError("protocol_version 不受支持")
        if self.contract_version != FLOW_FOUNDATION_CONTRACT_VERSION:
            raise FlowFoundationContractError("contract_version 不受支持")

    def to_dict(self) -> dict[str, str]:
        return {
            "execution_result": self.execution_result.value,
            "evidence_state": self.evidence_state.value,
            "resume_result": self.resume_result.value,
            "protocol_version": self.protocol_version,
            "contract_version": self.contract_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FlowTriStateOutcome:
        try:
            return cls(
                execution_result=FlowExecutionResult(payload["execution_result"]),
                evidence_state=FlowEvidenceState(payload["evidence_state"]),
                resume_result=FlowResumeResult(payload["resume_result"]),
                protocol_version=str(
                    payload.get("protocol_version") or FLOW_FOUNDATION_PROTOCOL_VERSION
                ),
                contract_version=str(
                    payload.get("contract_version") or FLOW_FOUNDATION_CONTRACT_VERSION
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FlowFoundationContractError("三项结果载荷无效") from exc


def reject_collapsed_success_payload(payload: Mapping[str, Any]) -> None:
    """拒绝用单一 success／status 冒充三项结果。"""

    if "execution_result" in payload and "evidence_state" in payload and "resume_result" in payload:
        return
    collapsed = payload.get("success")
    if collapsed is True or str(payload.get("status") or "").casefold() in {
        "success",
        "succeeded",
        "ok",
    }:
        raise FlowFoundationContractError(
            "禁止用单一 success／status 覆盖 execution／evidence／resume"
        )


# ---------------------------------------------------------------------------
# P0.2／P0.3 App 能力与平台／Surface 边界
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class AppFlowCapabilityRow:
    """App 能力矩阵一行：接口存在 ≠ 能调用 ≠ 本次证据充分。"""

    capability_id: str
    status: CapabilityStatus
    notes: str = ""

    def __post_init__(self) -> None:
        require_text(self.capability_id, "capability_id")
        if not isinstance(self.status, CapabilityStatus):
            raise FlowFoundationContractError("capability status 无效")


# 设计 §10.3：进度／时长等不可默认假设。
DEFAULT_APP_FLOW_CAPABILITY_MATRIX: tuple[AppFlowCapabilityRow, ...] = (
    AppFlowCapabilityRow(
        capability_id="device_screenshot",
        status=CapabilityStatus.READY,
        notes="设备截图",
    ),
    AppFlowCapabilityRow(
        capability_id="uia_tree",
        status=CapabilityStatus.PARTIAL,
        notes="可访问树；不保证与截图同帧",
    ),
    AppFlowCapabilityRow(
        capability_id="window_process_identity",
        status=CapabilityStatus.READY,
        notes="package／activity／窗口／进程身份",
    ),
    AppFlowCapabilityRow(
        capability_id="media_playback_position",
        status=CapabilityStatus.PARTIAL,
        notes="UIA 不保证进度／时长／媒体 URL",
    ),
    AppFlowCapabilityRow(
        capability_id="media_playback_progress_proof",
        status=CapabilityStatus.UNSUPPORTED,
        notes="无可靠进度增长事实源时不得启动正式播放验证流程",
    ),
    AppFlowCapabilityRow(
        capability_id="webview_dom_debug",
        status=CapabilityStatus.UNSUPPORTED,
        notes="不默认具备 WebView 调试接口",
    ),
    AppFlowCapabilityRow(
        capability_id="clear_app_data_as_return",
        status=CapabilityStatus.UNSUPPORTED,
        notes="清数据改变容器与账号，不能当普通返回",
    ),
    AppFlowCapabilityRow(
        capability_id="warm_restore",
        status=CapabilityStatus.PARTIAL,
        notes="不得默认视为能力可用；合同授权后才用",
    ),
    AppFlowCapabilityRow(
        capability_id="unauthorized_deep_link",
        status=CapabilityStatus.UNSUPPORTED,
        notes="未授权 deep link 禁止",
    ),
    AppFlowCapabilityRow(
        capability_id="payment_or_account_mutation",
        status=CapabilityStatus.UNSUPPORTED,
        notes="基建不自动开放支付／账号变更",
    ),
)


_PLAYBACK_PROOF_CAPABILITY_ID = "media_playback_progress_proof"

# 设计 §1 裁决 9：试点与流程不得自动开放的能力。
FORBIDDEN_AUTO_OPEN_CAPABILITY_IDS = frozenset(
    {
        "clear_app_data_as_return",
        "unauthorized_deep_link",
        "payment_or_account_mutation",
    }
)


def app_capability_status(
    capability_id: str,
    *,
    matrix: tuple[AppFlowCapabilityRow, ...] = DEFAULT_APP_FLOW_CAPABILITY_MATRIX,
) -> CapabilityStatus:
    for row in matrix:
        if row.capability_id == capability_id:
            return row.status
    return CapabilityStatus.UNSUPPORTED


def assert_playback_proof_flow_allowed(
    *,
    matrix: tuple[AppFlowCapabilityRow, ...] = DEFAULT_APP_FLOW_CAPABILITY_MATRIX,
) -> None:
    """规则若要求证明播放进度增长，App 无 ready 事实源则禁止启动正式判定流程。"""

    status = app_capability_status(_PLAYBACK_PROOF_CAPABILITY_ID, matrix=matrix)
    if status is not CapabilityStatus.READY:
        raise FlowFoundationContractError(
            "App 无可靠播放进度事实源：不得启动正式播放验证流程"
            f"（capability={_PLAYBACK_PROOF_CAPABILITY_ID} status={status.value}）"
        )


def assert_pilot_capabilities_allowed(
    capability_ids: tuple[str, ...],
    *,
    matrix: tuple[AppFlowCapabilityRow, ...] = DEFAULT_APP_FLOW_CAPABILITY_MATRIX,
) -> None:
    """试点不得依赖基建自动开放的高风险／不支持能力。"""

    for capability_id in capability_ids:
        if capability_id in FORBIDDEN_AUTO_OPEN_CAPABILITY_IDS:
            raise FlowFoundationContractError(
                f"试点禁止使用自动开放能力: {capability_id}"
            )
        if app_capability_status(capability_id, matrix=matrix) is CapabilityStatus.UNSUPPORTED:
            raise FlowFoundationContractError(
                f"试点能力 unsupported: {capability_id}"
            )


def resolve_app_device_conflict_id(
    *,
    device_id: str,
    surface_kind: SurfaceKind,
    webview_surface: bool = False,
) -> str:
    """App 原生与 WebView 共用同一设备冲突域；Surface 不能换出另一把锁。"""

    device = require_text(device_id, "device_id")
    if not isinstance(surface_kind, SurfaceKind):
        raise FlowFoundationContractError("surface_kind 无效")
    if surface_kind is SurfaceKind.ANDROID or webview_surface:
        # WebView 仍归 App 设备资源，冲突键仅为 device_id。
        return device
    raise FlowFoundationContractError(
        "App 流程设备冲突域仅接受 android Surface 或 App WebView"
    )


def assert_target_platform_surface_separation(
    *,
    target_platform: TargetPlatform | str,
    surface_kind: SurfaceKind,
) -> None:
    """业务平台与执行 Surface 分开校验；App 任务不得因 WebView 改成 web／h5。"""

    platform = (
        target_platform
        if isinstance(target_platform, TargetPlatform)
        else TargetPlatform(str(target_platform).strip().casefold())
    )
    if platform is TargetPlatform.APP and surface_kind is SurfaceKind.WEB:
        raise FlowFoundationContractError(
            "App 任务内的 WebView 不得将 target_platform 改为 web，也不另开设备冲突域"
        )


# ---------------------------------------------------------------------------
# P0.1 唯一动作入口声明（嵌入既有调度，禁止第二循环）
# ---------------------------------------------------------------------------

APP_FLOW_SINGLE_ACTION_ENTRY = "capture_coordinator"
APP_FLOW_FORBIDDEN_PARALLEL_LOOPS = frozenset(
    {
        "standalone_adb_worker_loop",
        "parallel_cloud_phone_driver_loop",
        "flow_private_device_scheduler",
    }
)


def assert_single_app_action_entry(entry_name: str) -> None:
    name = require_text(entry_name, "entry_name")
    if name in APP_FLOW_FORBIDDEN_PARALLEL_LOOPS:
        raise FlowFoundationContractError(
            f"禁止与主探索竞争的设备执行循环: {name}"
        )
    if name != APP_FLOW_SINGLE_ACTION_ENTRY:
        raise FlowFoundationContractError(
            f"App 流程动作必须经 {APP_FLOW_SINGLE_ACTION_ENTRY}，收到: {name}"
        )


# ---------------------------------------------------------------------------
# P1 采集归属与承诺（设计 §8／§9）
# ---------------------------------------------------------------------------


class CaptureReservationStatus(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    UPLOADING = "uploading"
    VERIFIED = "verified"
    ATTACHED = "attached"
    MISSING = "missing"
    EXPIRED = "expired"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, kw_only=True)
class CaptureOwnershipBinding:
    """Capture 原始归属；错绑拒绝（T11）。"""

    task_id: str
    capture_id: str
    owner_phase_run_id: str | None = None
    owner_execution_segment_id: str | None = None
    action_execution_id: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        require_text(self.task_id, "task_id")
        require_text(self.capture_id, "capture_id")


def assert_capture_binding_matches(
    expected: CaptureOwnershipBinding,
    *,
    task_id: str,
    capture_id: str,
    action_execution_id: str | None = None,
    session_id: str | None = None,
    phase_run_id: str | None = None,
    execution_segment_id: str | None = None,
) -> None:
    if task_id != expected.task_id or capture_id != expected.capture_id:
        raise FlowFoundationContractError("跨任务或 Capture 身份不匹配，拒绝归属")
    if (
        expected.action_execution_id is not None
        and action_execution_id != expected.action_execution_id
    ):
        raise FlowFoundationContractError("同任务同 Session 关联错误动作，拒绝")
    if expected.session_id is not None and session_id != expected.session_id:
        raise FlowFoundationContractError("Session 归属不匹配，拒绝")
    if (
        expected.owner_phase_run_id is not None
        and phase_run_id != expected.owner_phase_run_id
    ):
        raise FlowFoundationContractError("phase_run 归属不匹配，拒绝")
    if (
        expected.owner_execution_segment_id is not None
        and execution_segment_id != expected.owner_execution_segment_id
    ):
        raise FlowFoundationContractError("execution_segment 归属不匹配，拒绝")


@dataclass(frozen=True, kw_only=True)
class CaptureReservation:
    """预分配采集身份与冻结上下文引用（尚无内容承诺）。"""

    capture_request_id: str
    capture_id: str
    task_id: str
    status: CaptureReservationStatus
    ownership: CaptureOwnershipBinding
    artifact_slots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.capture_request_id, "capture_request_id")
        require_text(self.capture_id, "capture_id")
        require_text(self.task_id, "task_id")
        if self.task_id != self.ownership.task_id or self.capture_id != self.ownership.capture_id:
            raise FlowFoundationContractError("预约身份与归属绑定不一致")
        if not isinstance(self.status, CaptureReservationStatus):
            raise FlowFoundationContractError("reservation status 无效")


@dataclass(frozen=True, kw_only=True)
class ArtifactCommitment:
    """实际采样后的内容承诺；无承诺不得进正式证据（T14）。"""

    capture_request_id: str
    capture_id: str
    artifact_slot: str
    content_digest: str
    content_length: int
    produced_under_fencing_token: str

    def __post_init__(self) -> None:
        require_text(self.capture_request_id, "capture_request_id")
        require_text(self.capture_id, "capture_id")
        require_text(self.artifact_slot, "artifact_slot")
        require_text(self.content_digest, "content_digest")
        require_text(self.produced_under_fencing_token, "produced_under_fencing_token")
        if int(self.content_length) < 0:
            raise FlowFoundationContractError("content_length 无效")


def assert_formal_evidence_has_commitment(
    *,
    reservation: CaptureReservation,
    commitment: ArtifactCommitment | None,
) -> None:
    if reservation.status in {
        CaptureReservationStatus.MISSING,
        CaptureReservationStatus.EXPIRED,
        CaptureReservationStatus.QUARANTINED,
    }:
        raise FlowFoundationContractError("预约已失效，不能进入正式证据")
    if commitment is None:
        raise FlowFoundationContractError("仅有预约、没有内容承诺，不能进入正式证据")
    if commitment.capture_request_id != reservation.capture_request_id:
        raise FlowFoundationContractError("内容承诺与预约 capture_request_id 不一致")
    if commitment.capture_id != reservation.capture_id:
        raise FlowFoundationContractError("内容承诺与预约 capture_id 不一致")


def assert_late_upload_stays_with_owner(
    *,
    material_owner_task_id: str,
    receiving_runtime_task_id: str,
) -> None:
    """A 材料在 B 运行时上传仍归 A（T13）。"""

    if material_owner_task_id != receiving_runtime_task_id:
        raise FlowFoundationContractError(
            "迟到材料不得改归当前运行任务；始终归属原预约任务"
        )


def assert_pure_observation_has_no_action_id(action_execution_id: str | None) -> None:
    """纯观察不得伪造 ActionExecution（T21）。"""

    if action_execution_id is not None and str(action_execution_id).strip():
        raise FlowFoundationContractError(
            "纯观察的 action_execution_id 必须为空，禁止伪造点击动作"
        )


# ---------------------------------------------------------------------------
# P1 设备执行权（设计 §7.2）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class DeviceExecutionPermit:
    """有期限执行许可投影（领域侧校验用）。"""

    device_id: str
    resource_id: str
    fencing_token: str
    session_id: str
    execution_segment_id: str
    task_id: str
    revoked: bool = False

    def __post_init__(self) -> None:
        require_text(self.device_id, "device_id")
        require_text(self.resource_id, "resource_id")
        require_text(self.fencing_token, "fencing_token")
        require_text(self.session_id, "session_id")
        require_text(self.execution_segment_id, "execution_segment_id")
        require_text(self.task_id, "task_id")


def assert_execution_permit_active(permit: DeviceExecutionPermit) -> None:
    if permit.revoked:
        raise FlowFoundationContractError("执行许可已撤销，拒绝新动作与越权写入")


def assert_fencing_token_matches(
    permit: DeviceExecutionPermit,
    *,
    presented_fencing_token: str,
) -> None:
    assert_execution_permit_active(permit)
    token = require_text(presented_fencing_token, "presented_fencing_token")
    if token != permit.fencing_token:
        raise FlowFoundationContractError("fencing token 不匹配，拒绝执行／写库")


def assert_single_device_executor(
    *,
    device_id: str,
    active_session_id: str,
    claimant_session_id: str,
) -> None:
    require_text(device_id, "device_id")
    if active_session_id != claimant_session_id:
        raise FlowFoundationContractError(
            "同一 device_id 已有有效动作执行者，不能以换 Session 绕锁"
        )


@dataclass(frozen=True, kw_only=True)
class DeviceLeaseHandoff:
    """跨 Session 租约承接：同设备、轮转 fence、记录前驱（T27）。"""

    device_id: str
    task_id: str
    predecessor_session_id: str
    successor_session_id: str
    predecessor_fencing_token: str
    successor_fencing_token: str
    predecessor_channel_silenced: bool

    def __post_init__(self) -> None:
        require_text(self.device_id, "device_id")
        require_text(self.task_id, "task_id")
        require_text(self.predecessor_session_id, "predecessor_session_id")
        require_text(self.successor_session_id, "successor_session_id")
        require_text(self.predecessor_fencing_token, "predecessor_fencing_token")
        require_text(self.successor_fencing_token, "successor_fencing_token")
        if self.predecessor_session_id == self.successor_session_id:
            raise FlowFoundationContractError("租约承接必须换到新 Session")
        if self.predecessor_fencing_token == self.successor_fencing_token:
            raise FlowFoundationContractError("租约承接必须轮转 fencing token")


def assert_lease_handoff_safe(handoff: DeviceLeaseHandoff) -> None:
    if not handoff.predecessor_channel_silenced:
        raise FlowFoundationContractError(
            "旧命令通道未静默前禁止跨 Session 租约承接"
        )


def decide_unknown_action_replay(
    *,
    outcome_unknown: bool,
    proved_not_occurred: bool,
) -> str:
    """T06：结果未知先核验；无法证明未发生则不重放。返回 allow_replay｜forbid_replay。"""

    if not outcome_unknown:
        return "allow_replay"
    if proved_not_occurred:
        return "allow_replay"
    return "forbid_replay"


def build_capture_request_identity(
    *,
    task_id: str,
    phase_run_id: str,
    checkpoint_key: str,
    sample_index: int,
    producer_epoch: str,
) -> str:
    """稳定采集请求身份（内容寻址），用于预约幂等。"""

    return "capture-request:" + stable_protocol_hash(
        {
            "contract_version": FLOW_FOUNDATION_CONTRACT_VERSION,
            "task_id": require_text(task_id, "task_id"),
            "phase_run_id": require_text(phase_run_id, "phase_run_id"),
            "checkpoint_key": require_text(checkpoint_key, "checkpoint_key"),
            "sample_index": int(sample_index),
            "producer_epoch": require_text(producer_epoch, "producer_epoch"),
        }
    )
