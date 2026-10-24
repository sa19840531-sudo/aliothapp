"""跨端流程基建 P3：连续观察与证据质量（联调清单 P3）。

设计依据：2026-10-22 §9–§10、§11.1 TimedObservation v3。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from services.api.app.domain.exploration.flow_foundation import (
    FlowFoundationContractError,
    app_capability_status,
    assert_playback_proof_flow_allowed,
)
from services.api.app.domain.interaction.capabilities import CapabilityStatus
from services.api.app.domain.protocol import require_text

FLOW_EVIDENCE_CONTRACT_VERSION = "flow-foundation.p3.evidence.v1"


class ObservationClockDomain(StrEnum):
    """分时钟域；不可直接相减（T17／T18）。"""

    DEVICE = "device"
    BROWSER = "browser"
    SERVER = "server"
    PRODUCER_MONOTONIC = "producer_monotonic"
    PERSISTED_AT_LEGACY = "persisted_at_legacy"


class SampleIntegrityKind(StrEnum):
    OBSERVED = "observed"
    MISSING = "missing"
    ARTIFACT_GAP = "artifact_gap"
    TRIMMED = "trimmed"


@dataclass(frozen=True, kw_only=True)
class ResourceEventSeqAllocation:
    """采样入口分配的实际事件序；不靠上传后排序（T17）。"""

    resource_id: str
    producer_epoch: str
    resource_event_seq: int
    sample_index: int
    clock_domain: ObservationClockDomain

    def __post_init__(self) -> None:
        require_text(self.resource_id, "resource_id")
        require_text(self.producer_epoch, "producer_epoch")
        if int(self.resource_event_seq) < 1:
            raise FlowFoundationContractError("resource_event_seq 必须从 1 起")
        if int(self.sample_index) < 0:
            raise FlowFoundationContractError("sample_index 无效")
        if not isinstance(self.clock_domain, ObservationClockDomain):
            raise FlowFoundationContractError("clock_domain 无效")
        if self.clock_domain is ObservationClockDomain.PERSISTED_AT_LEGACY:
            raise FlowFoundationContractError(
                "采样入口不得用 persisted_at_legacy 充当 observed 时钟域"
            )


def assert_observed_at_not_persisted_legacy(
    *,
    clock_domain: ObservationClockDomain,
    field_name: str = "observed_at",
) -> None:
    """写库／调度时间 ≠ observed_at；legacy 只打标不回填（T17／T18）。"""

    if clock_domain is ObservationClockDomain.PERSISTED_AT_LEGACY:
        raise FlowFoundationContractError(
            f"{field_name} 不得回填为 persisted_at_legacy；须另字段标注"
        )


@dataclass(frozen=True, kw_only=True)
class SampleFrameRef:
    """帧身份：capture_id + sample_index；缺号原样保留（T16／T19）。"""

    capture_id: str
    sample_index: int
    content_digest: str
    integrity: SampleIntegrityKind = SampleIntegrityKind.OBSERVED
    missing_reason: str = ""

    def __post_init__(self) -> None:
        require_text(self.capture_id, "capture_id")
        require_text(self.content_digest, "content_digest")
        if int(self.sample_index) < 0:
            raise FlowFoundationContractError("sample_index 无效")
        if not isinstance(self.integrity, SampleIntegrityKind):
            raise FlowFoundationContractError("integrity 无效")
        if self.integrity is SampleIntegrityKind.MISSING and not str(
            self.missing_reason
        ).strip():
            raise FlowFoundationContractError("缺帧必须保留原因")


@dataclass
class ObservationSequenceDraft:
    """有界观察序列草稿：同字节不同样本保留两事件；禁止压缩编号。"""

    series_id: str
    frames: list[SampleFrameRef] = field(default_factory=list)
    sealed_revision: int | None = None
    previous_revision: int | None = None

    def __post_init__(self) -> None:
        require_text(self.series_id, "series_id")

    def append_frame(self, frame: SampleFrameRef) -> None:
        if self.sealed_revision is not None:
            raise FlowFoundationContractError(
                "已封存序列不得原地追加；须新 revision"
            )
        for existing in self.frames:
            if (
                existing.capture_id == frame.capture_id
                and existing.sample_index == frame.sample_index
                and existing != frame
            ):
                raise FlowFoundationContractError(
                    "同 capture_id+sample_index 内容冲突"
                )
        # 同 digest 不同 sample_index 必须保留为两事件。
        self.frames.append(frame)

    def mark_missing(self, *, sample_index: int, reason: str) -> None:
        self.append_frame(
            SampleFrameRef(
                capture_id=f"{self.series_id}:gap",
                sample_index=sample_index,
                content_digest="missing",
                integrity=SampleIntegrityKind.MISSING,
                missing_reason=reason,
            )
        )

    def observed_indices(self) -> tuple[int, ...]:
        return tuple(sorted({f.sample_index for f in self.frames}))


def assert_missing_indices_preserved(
    *,
    planned_indices: tuple[int, ...],
    frames: tuple[SampleFrameRef, ...],
) -> None:
    """缺帧保留原 index，不得压缩编号伪装连续（T19）。"""

    present = {f.sample_index for f in frames}
    for index in planned_indices:
        if index not in present:
            raise FlowFoundationContractError(
                f"预定 sample_index={index} 缺失且未登记缺口"
            )
    # 禁止把高 index「下压」填洞。
    if present and planned_indices:
        if max(present) < max(planned_indices):
            raise FlowFoundationContractError("样本编号被压缩，禁止伪装连续")


def assert_app_screenshot_tree_not_same_frame_assumption(
    *,
    assume_same_frame: bool,
) -> None:
    """App 截图与 UIA 树未必同帧（P3.4）。"""

    if assume_same_frame:
        raise FlowFoundationContractError(
            "App 不得假设截图与可访问树同帧"
        )


def assert_app_partial_media_not_playback_proof() -> None:
    """partial 进度能力不得冒充正式播放验证（T03／P3.4）。"""

    status = app_capability_status("media_playback_position")
    if status is CapabilityStatus.PARTIAL:
        try:
            assert_playback_proof_flow_allowed()
        except FlowFoundationContractError:
            return
        raise FlowFoundationContractError(
            "partial 媒体进度不得给出正式播放类通过"
        )
    assert_playback_proof_flow_allowed()


class CaptureAttemptKind(StrEnum):
    """重采 vs 重传（T28）。"""

    RECAPTURE = "recapture"
    REUPLOAD = "reupload"


@dataclass(frozen=True, kw_only=True)
class CaptureAttemptIdentity:
    kind: CaptureAttemptKind
    capture_request_id: str
    prior_capture_request_id: str | None = None
    new_observation_window: bool = False

    def __post_init__(self) -> None:
        require_text(self.capture_request_id, "capture_request_id")
        if not isinstance(self.kind, CaptureAttemptKind):
            raise FlowFoundationContractError("attempt kind 无效")
        if self.kind is CaptureAttemptKind.RECAPTURE:
            if not self.new_observation_window:
                raise FlowFoundationContractError(
                    "重采必须新建观察窗口与身份"
                )
            if self.prior_capture_request_id == self.capture_request_id:
                raise FlowFoundationContractError(
                    "重采不得沿用原 capture_request_id"
                )
        if self.kind is CaptureAttemptKind.REUPLOAD:
            if self.new_observation_window:
                raise FlowFoundationContractError(
                    "重传不得宣称新观察窗口"
                )
            if (
                self.prior_capture_request_id is not None
                and self.prior_capture_request_id != self.capture_request_id
            ):
                raise FlowFoundationContractError(
                    "重传必须沿用原 capture_request_id"
                )


class TimedObservationProtocolVersion(StrEnum):
    V1 = "v1"
    V2 = "v2"
    V3 = "v3"


def assert_timed_observation_v3_enabled(
    *,
    writer_supports_v3: bool,
    reader_supports_v3: bool,
    scheduler_supports_v3: bool,
    adapter_supports_v3: bool,
    requested_version: TimedObservationProtocolVersion | str,
) -> TimedObservationProtocolVersion:
    """仅四端均支持时启用 v3；旧协议不静默变浏览器请求（T23）。"""

    version = (
        requested_version
        if isinstance(requested_version, TimedObservationProtocolVersion)
        else TimedObservationProtocolVersion(str(requested_version).strip().casefold())
    )
    if version is TimedObservationProtocolVersion.V3:
        if not all(
            (
                writer_supports_v3,
                reader_supports_v3,
                scheduler_supports_v3,
                adapter_supports_v3,
            )
        ):
            raise FlowFoundationContractError(
                "TimedObservation v3 须 writer／reader／调度／适配器均声明支持"
            )
    return version


@dataclass(frozen=True, kw_only=True)
class EvidenceSequenceSeal:
    """封存后补齐→新版本；旧 InputSet／结论不变（T20）。"""

    series_id: str
    revision: int
    previous_revision: int | None
    input_set_id: str
    task_terminal: bool = False

    def __post_init__(self) -> None:
        require_text(self.series_id, "series_id")
        require_text(self.input_set_id, "input_set_id")
        if int(self.revision) < 1:
            raise FlowFoundationContractError("revision 无效")
        if self.previous_revision is not None and int(self.previous_revision) >= int(
            self.revision
        ):
            raise FlowFoundationContractError("新 revision 必须大于 previous")


def assert_late_fill_creates_new_revision(
    *,
    sealed: EvidenceSequenceSeal,
    late_fill: bool,
    mutate_old_input_set: bool,
    reopen_terminal_task: bool,
) -> EvidenceSequenceSeal:
    """封存后补齐只能新版本；不得复活已结束任务。"""

    if not late_fill:
        return sealed
    if mutate_old_input_set:
        raise FlowFoundationContractError(
            "封存后补齐不得修改旧 InputSet／结论"
        )
    if reopen_terminal_task or sealed.task_terminal:
        if reopen_terminal_task:
            raise FlowFoundationContractError(
                "迟到补齐不得复活已结束任务"
            )
    return EvidenceSequenceSeal(
        series_id=sealed.series_id,
        revision=int(sealed.revision) + 1,
        previous_revision=sealed.revision,
        input_set_id=f"{sealed.input_set_id}:r{int(sealed.revision) + 1}",
        task_terminal=sealed.task_terminal,
    )
