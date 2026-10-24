"""按业务目标平台选择最小证据采集策略。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from services.api.app.domain.interaction.captures import (
    CaptureDirective,
    CaptureProfile,
)
from services.api.app.domain.interaction.enums import InteractionOperation, SurfaceKind
from services.api.app.domain.task.targets import TaskTargetPlatform

# App 主 Capture 的正式制品词汇。Profile、Driver、Writer 和 Persistence
# 必须复用这份合同，不得各自维护可漂移的默认白名单。
APP_MAIN_CAPTURE_ARTIFACT_TYPES = (
    "screenshot",
    "action_trace",
    "ui_tree",
    "view_hierarchy",
    "ui_snapshot",
    "visible_text",
    "media_state",
    "device_log",
    "network_trace",
    # 由 network_trace 投影的 Web 兼容网络事件；无代理时为空 entries 负证据
    "har",
    # 真实连拍时序（timed_capture）；禁止复制同图冒充多帧
    "temporal_trace",
    "fault_artifact",
)
APP_MAIN_CAPTURE_REQUIRED_ARTIFACT_TYPES = (
    "screenshot",
    "action_trace",
)
# 动作前只存在页面状态事实；动作轨迹由执行动作产生，属于动作后证据。
APP_MAIN_CAPTURE_BEFORE_REQUIRED_ARTIFACT_TYPES = ("screenshot",)
APP_MAIN_CAPTURE_BEFORE_ARTIFACT_TYPES = (
    "screenshot",
    "ui_tree",
    "view_hierarchy",
    "ui_snapshot",
    "visible_text",
    "media_state",
)
APP_MAIN_CAPTURE_SOFT_OPTIONAL_ARTIFACT_TYPES = (
    "ui_tree",
    "view_hierarchy",
    "ui_snapshot",
    "visible_text",
    "media_state",
    "device_log",
    "network_trace",
    "har",
    "temporal_trace",
    "fault_artifact",
)
# 预留给未来显式 Provider 合同；当前默认声明的所有制品都必须能够正式落库。
APP_PROVIDER_OPTIONAL_ARTIFACT_TYPES: tuple[str, ...] = ()
APP_CAPTURE_ARTIFACT_TYPES = (
    *APP_MAIN_CAPTURE_ARTIFACT_TYPES,
    *APP_PROVIDER_OPTIONAL_ARTIFACT_TYPES,
)
APP_CAPTURE_ARTIFACT_CONTRACT_VERSION = "app-capture-artifact-contract.v3"
# 默认 after 不含 temporal_trace，避免每次 Capture 都连拍；TIMED_CAPTURE／显式请求才加入
APP_DEFAULT_AFTER_ARTIFACT_TYPES = tuple(
    item for item in APP_MAIN_CAPTURE_ARTIFACT_TYPES if item != "temporal_trace"
)

_WEB_ARTIFACTS = ("screenshot", "dom", "visible_text", "ui_snapshot")
# App 探索默认：截图 + 动作轨迹 + 有界 UI/日志/网络（失败可降级）
_APP_ARTIFACTS = APP_DEFAULT_AFTER_ARTIFACT_TYPES
_APP_BEFORE_ARTIFACTS = APP_MAIN_CAPTURE_BEFORE_ARTIFACT_TYPES
_APP_PROBE_ARTIFACTS = (
    "screenshot",
    "ui_tree",
    "view_hierarchy",
    "ui_snapshot",
    "visible_text",
    "media_state",
    "action_trace",
    "device_log",
    "network_trace",
    "har",
    "temporal_trace",
    "fault_artifact",
)
_APP_ALLOWED_ARTIFACTS = frozenset(APP_CAPTURE_ARTIFACT_TYPES)


@dataclass(frozen=True, slots=True)
class AppCaptureArtifactSelection:
    """一次 Capture 对 requested/produced/持久化制品的确定性裁决。"""

    role: str
    requested_artifact_types: tuple[str, ...]
    produced_artifact_types: tuple[str, ...]
    persisted_artifact_types: tuple[str, ...]
    missing_soft_optional_artifact_types: tuple[str, ...]
    provider_only_artifact_types: tuple[str, ...]
    required_artifact_types: tuple[str, ...]
    contract_version: str = APP_CAPTURE_ARTIFACT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.role not in {"before_action", "after_action", "observation"}:
            raise ValueError("App Capture artifact contract role 不受支持")
        if self.contract_version != APP_CAPTURE_ARTIFACT_CONTRACT_VERSION:
            raise ValueError("App Capture artifact contract version 不受支持")
        for field_name in (
            "requested_artifact_types",
            "produced_artifact_types",
            "persisted_artifact_types",
            "missing_soft_optional_artifact_types",
            "provider_only_artifact_types",
            "required_artifact_types",
        ):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or len(set(values)) != len(values):
                raise ValueError(f"{field_name} 必须是无重复 tuple")

    def to_metadata(self) -> dict[str, object]:
        """生成可写入 Capture Screenshot metadata 的状态摘要。"""

        return {
            "contract_version": self.contract_version,
            "role": self.role,
            "requested_artifact_types": list(self.requested_artifact_types),
            "produced_artifact_types": list(self.produced_artifact_types),
            "persisted_artifact_types": list(self.persisted_artifact_types),
            "required_artifact_types": list(self.required_artifact_types),
            "missing_soft_optional_artifact_types": list(self.missing_soft_optional_artifact_types),
            "provider_only_artifact_types": list(self.provider_only_artifact_types),
        }


@dataclass(frozen=True, slots=True)
class AppCaptureArtifactContract:
    """App Capture 唯一 required/soft-optional 与持久化边界合同。"""

    required_artifact_types: tuple[str, ...] = APP_MAIN_CAPTURE_REQUIRED_ARTIFACT_TYPES
    before_action_required_artifact_types: tuple[str, ...] = (
        APP_MAIN_CAPTURE_BEFORE_REQUIRED_ARTIFACT_TYPES
    )
    observation_required_artifact_types: tuple[str, ...] = ("screenshot",)
    soft_optional_artifact_types: tuple[str, ...] = (
        *APP_MAIN_CAPTURE_SOFT_OPTIONAL_ARTIFACT_TYPES,
        *APP_PROVIDER_OPTIONAL_ARTIFACT_TYPES,
    )
    persistable_artifact_types: tuple[str, ...] = APP_MAIN_CAPTURE_ARTIFACT_TYPES
    all_artifact_types: tuple[str, ...] = APP_CAPTURE_ARTIFACT_TYPES
    version: str = APP_CAPTURE_ARTIFACT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "required_artifact_types",
            "before_action_required_artifact_types",
            "observation_required_artifact_types",
            "soft_optional_artifact_types",
            "persistable_artifact_types",
            "all_artifact_types",
        ):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or len(set(values)) != len(values):
                raise ValueError(f"{field_name} 必须是无重复 tuple")
        all_types = set(self.all_artifact_types)
        if not set(self.required_artifact_types) <= all_types:
            raise ValueError("required_artifact_types 超出 App Capture 合同")
        if not set(self.before_action_required_artifact_types) <= all_types:
            raise ValueError("before_action_required_artifact_types 超出 App Capture 合同")
        if not set(self.observation_required_artifact_types) <= all_types:
            raise ValueError("observation_required_artifact_types 超出 App Capture 合同")
        if not set(self.soft_optional_artifact_types) <= all_types:
            raise ValueError("soft_optional_artifact_types 超出 App Capture 合同")
        if not set(self.persistable_artifact_types) <= all_types:
            raise ValueError("persistable_artifact_types 超出 App Capture 合同")
        persistable = set(self.persistable_artifact_types)
        if not set(self.required_artifact_types) <= persistable:
            raise ValueError("required_artifact_types 必须能够正式持久化")
        if not set(self.before_action_required_artifact_types) <= persistable:
            raise ValueError("before_action_required_artifact_types 必须能够正式持久化")
        if not set(self.observation_required_artifact_types) <= persistable:
            raise ValueError("observation required_artifact_types 必须能够正式持久化")
        if set(self.required_artifact_types) & set(self.soft_optional_artifact_types):
            raise ValueError("required 与 soft-optional 制品不得重叠")
        if set(self.before_action_required_artifact_types) & set(self.soft_optional_artifact_types):
            raise ValueError("before_action required 与 soft-optional 制品不得重叠")
        if self.version != APP_CAPTURE_ARTIFACT_CONTRACT_VERSION:
            raise ValueError("App Capture artifact contract version 不受支持")

    def required_for(self, role: str) -> tuple[str, ...]:
        """动作前要求截图，动作后要求截图+轨迹；Observation 只要求截图。"""

        if role == "before_action":
            return self.before_action_required_artifact_types
        if role == "after_action":
            return self.required_artifact_types
        if role == "observation":
            return self.observation_required_artifact_types
        raise ValueError("App Capture artifact contract role 不受支持")

    def select(
        self,
        *,
        role: str,
        requested: Sequence[str],
        produced: Sequence[str],
    ) -> AppCaptureArtifactSelection:
        """裁决产物集合；缺 soft-optional 只形成状态，不阻断主证据。"""

        required = self.required_for(role)
        requested_types = _unique_artifact_types(requested, "requested_artifact_types")
        produced_types = _unique_artifact_types(produced, "produced_artifact_types")
        all_types = set(self.all_artifact_types)
        unknown_requested = set(requested_types) - all_types
        if unknown_requested:
            raise ValueError(
                "App Capture requested 制品不受支持：" + ", ".join(sorted(unknown_requested))
            )
        unknown_produced = set(produced_types) - all_types
        if unknown_produced:
            raise ValueError(
                "App Capture produced 制品不受支持：" + ", ".join(sorted(unknown_produced))
            )
        if not set(required) <= set(requested_types):
            raise ValueError(
                "App Capture Profile 未声明 required 制品："
                + ", ".join(sorted(set(required) - set(requested_types)))
            )
        missing_required = set(required) - set(produced_types)
        if missing_required:
            raise ValueError(
                "App Capture 产物缺少 required 制品：" + ", ".join(sorted(missing_required))
            )
        produced_requested = set(produced_types) & set(requested_types)
        persisted = tuple(
            item
            for item in requested_types
            if item in produced_requested and item in self.persistable_artifact_types
        )
        missing_soft = tuple(
            item
            for item in requested_types
            if item in self.soft_optional_artifact_types and item not in produced_requested
        )
        provider_only = tuple(
            item
            for item in requested_types
            if item in self.soft_optional_artifact_types
            and item not in self.persistable_artifact_types
            and item in produced_requested
        )
        return AppCaptureArtifactSelection(
            role=role,
            requested_artifact_types=requested_types,
            produced_artifact_types=produced_types,
            persisted_artifact_types=persisted,
            missing_soft_optional_artifact_types=missing_soft,
            provider_only_artifact_types=provider_only,
            required_artifact_types=required,
        )


APP_CAPTURE_ARTIFACT_CONTRACT = AppCaptureArtifactContract()


def _unique_artifact_types(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    normalized = tuple(values)
    if any(not isinstance(item, str) or not item.strip() for item in normalized):
        raise ValueError(f"{field_name} 必须是非空字符串集合")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} 不得重复")
    return normalized


class CaptureProfilePolicy:
    """App 默认采集截图/动作轨迹，并请求 UI Tree；禁止把 UI Tree 伪装成 DOM。"""

    @staticmethod
    def for_platform(platform: TaskTargetPlatform) -> CaptureProfile:
        if not isinstance(platform, TaskTargetPlatform):
            raise ValueError("target_platform 不受支持")
        if platform is TaskTargetPlatform.APP:
            return CaptureProfile(
                profile_id="app-observation-platform-v1",
                before=CaptureDirective(
                    role="before_action",
                    artifact_types=_APP_BEFORE_ARTIFACTS,
                ),
                after=CaptureDirective(
                    role="after_action",
                    artifact_types=APP_DEFAULT_AFTER_ARTIFACT_TYPES,
                ),
            )
        return CaptureProfilePolicy.for_surface(SurfaceKind.WEB)

    @staticmethod
    def for_surface(
        surface_kind: SurfaceKind,
        operation: InteractionOperation | None = None,
    ) -> CaptureProfile:
        """根据 Runtime Surface 和动作选择最小 before/after 证据。"""
        if not isinstance(surface_kind, SurfaceKind):
            raise ValueError("surface_kind 不受支持")
        if operation is not None and not isinstance(operation, InteractionOperation):
            raise ValueError("operation 不受支持")
        operation_name = operation.value if operation is not None else "observation"
        if surface_kind is not SurfaceKind.WEB:
            after_artifacts = list(_APP_ARTIFACTS)
            if operation is InteractionOperation.TIMED_CAPTURE:
                if "temporal_trace" not in after_artifacts:
                    after_artifacts.append("temporal_trace")
            return CaptureProfile(
                profile_id=f"app-{operation_name}-v1",
                before=CaptureDirective(
                    role="before_action",
                    artifact_types=_APP_BEFORE_ARTIFACTS,
                ),
                after=CaptureDirective(
                    role="after_action",
                    artifact_types=tuple(after_artifacts),
                ),
            )
        after_artifacts = list(_WEB_ARTIFACTS)
        before_artifacts = list(_WEB_ARTIFACTS)
        if operation is InteractionOperation.TIMED_CAPTURE:
            after_artifacts.append("temporal_trace")
        if operation is InteractionOperation.SEEK_MEDIA:
            before_artifacts.append("media_state")
            after_artifacts.append("media_state")
        if operation is InteractionOperation.NETWORK_FAULT:
            after_artifacts.append("har")
        after_artifacts.append("action_trace")
        return CaptureProfile(
            profile_id=f"web-{operation_name}-v1",
            before=CaptureDirective(
                role="before_action",
                artifact_types=tuple(before_artifacts),
            ),
            after=CaptureDirective(
                role="after_action",
                artifact_types=tuple(after_artifacts),
            ),
        )

    @staticmethod
    def for_app_probe(
        *,
        operation: InteractionOperation | None = None,
        required_artifact_types: tuple[str, ...] = (),
    ) -> CaptureProfile:
        """Probe 补采：角色必须与 App Capture 持久化白名单对齐（before_action/after_action）。"""
        if operation is InteractionOperation.SEEK_MEDIA:
            # App Official Runtime 当前只能从 Accessibility 层投影降级媒体候选，
            # 没有播放进度/时长等正式 media_state；Probe 不得把它当作可执行证据。
            raise ValueError(
                "App Probe SEEK_MEDIA 需要正式 media_state；当前 Official App Runtime 未提供"
            )
        operation_name = operation.value if operation is not None else "observe"
        requested = tuple(
            dict.fromkeys(
                [
                    *_APP_PROBE_ARTIFACTS,
                    *(item for item in required_artifact_types if item in _APP_ALLOWED_ARTIFACTS),
                ]
            )
        )
        unsupported = set(required_artifact_types) - _APP_ALLOWED_ARTIFACTS
        if unsupported:
            raise ValueError("App Probe 不允许的证据类型：" + ", ".join(sorted(unsupported)))
        return CaptureProfile(
            profile_id=f"app-probe-{operation_name}-v1",
            before=CaptureDirective(
                role="before_action",
                artifact_types=APP_MAIN_CAPTURE_BEFORE_ARTIFACT_TYPES,
                inherit_parent_semantics=True,
            ),
            after=CaptureDirective(
                role="after_action",
                artifact_types=requested,
            ),
        )

    @staticmethod
    def app_allowed_artifact_types() -> frozenset[str]:
        return _APP_ALLOWED_ARTIFACTS
