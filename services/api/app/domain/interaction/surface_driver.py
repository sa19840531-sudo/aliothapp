"""Web 与 App 共用的 SurfaceDriver 适配合同。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from services.api.app.domain.interaction.action_intent import ActionIntent
from services.api.app.domain.interaction.capabilities import SurfaceCapabilityManifest
from services.api.app.domain.interaction.captures import CaptureProfile
from services.api.app.domain.interaction.enums import SurfaceKind
from services.api.app.domain.interaction.validation import (
    RUNTIME_HANDLE_KEYS,
    InteractionValidationError,
    freeze_mapping,
    require_text,
    thaw_mapping,
)
from services.api.app.domain.protocol import freeze_json_mapping


class SurfaceDriverError(RuntimeError):
    """驱动可透传的受控失败；异常正文不属于持久化执行结果。"""

    def __init__(self, message: str, *, error_code: str, retryable: bool) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


def app_primary_surface_ref(session_id: str) -> str:
    """返回 App 会话在持久化层和 Adapter 间共享的稳定公开身份。"""
    return f"app-surface:{require_text(session_id, 'session_id')}"


def web_primary_surface_ref(session_id: str) -> str:
    """返回 Web 会话在持久化层和 Adapter 间共享的稳定公开身份。

    工作流侧注册主 Surface 与命令闸门侧续期必须用同一个值：qc_surface_ref 上
    除主键外还有 UNIQUE(session_id, surface_key)，两侧算法不一致时后写入的一方
    绕过 ON CONFLICT (surface_ref_id) 幂等分支，直接撞唯一约束报 duplicate key。
    """
    return f"web-primary:{require_text(session_id, 'session_id')}"


@dataclass(frozen=True, kw_only=True)
class SurfaceSessionSpec:
    """创建平台会话的稳定声明；凭证只允许携带引用。"""

    task_id: str
    session_id: str
    surface_kind: SurfaceKind
    account_environment_ref: str | None = None
    platform_build_ref: str | None = None
    device_ref: str | None = None
    # App 设备资源租约公开身份；token 不进入协议。
    device_lease_id: str | None = None
    device_lease_fencing_token: int | None = None
    # App 一等恢复策略：warm_preferred | force_stop | clear_data | none
    reset_policy: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_text(self.task_id, "task_id")
        require_text(self.session_id, "session_id")
        if not isinstance(self.surface_kind, SurfaceKind):
            raise InteractionValidationError("surface_kind 不受支持")
        for field_name in (
            "account_environment_ref",
            "platform_build_ref",
            "device_ref",
            "device_lease_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                require_text(value, field_name)
        reset_policy = self.reset_policy
        if reset_policy is not None:
            reset_policy = require_text(reset_policy, "reset_policy").casefold()
            if reset_policy not in _APP_RESET_POLICIES:
                raise InteractionValidationError(
                    "reset_policy 仅支持 warm_preferred/force_stop/clear_data/none"
                )
            object.__setattr__(self, "reset_policy", reset_policy)
        options = thaw_mapping(self.options) if self.options else {}
        if not isinstance(options, dict):
            options = dict(self.options or {})
        else:
            options = dict(options)
        if self.surface_kind is SurfaceKind.ANDROID:
            options = _normalize_android_session_options(
                options,
                device_ref=self.device_ref,
                reset_policy=self.reset_policy,
            )
            if self.reset_policy is None and options.get("reset_policy"):
                object.__setattr__(self, "reset_policy", str(options["reset_policy"]))
        object.__setattr__(
            self,
            "options",
            freeze_mapping(
                options,
                "options",
                forbidden_keys=RUNTIME_HANDLE_KEYS
                | frozenset({"password", "access_token", "refresh_token", "cookie"}),
            ),
        )
        if self.device_lease_fencing_token is not None and (
            isinstance(self.device_lease_fencing_token, bool)
            or not isinstance(self.device_lease_fencing_token, int)
            or self.device_lease_fencing_token < 1
        ):
            raise InteractionValidationError(
                "device_lease_fencing_token 必须为正整数"
            )
        if (self.device_lease_id is None) != (
            self.device_lease_fencing_token is None
        ):
            raise InteractionValidationError(
                "设备租约 ID 与 fencing token 必须同时提供"
            )


_APP_RESET_POLICIES = frozenset(
    {"warm_preferred", "force_stop", "clear_data", "none"}
)


def _normalize_android_session_options(
    options: dict[str, Any],
    *,
    device_ref: str | None,
    reset_policy: str | None,
) -> dict[str, Any]:
    """补齐 App 业务会话身份；同设备不等于同业务会话。"""
    normalized = dict(options)
    device = str(device_ref or normalized.get("device_ref") or "android:default").strip()
    normalized.setdefault("lifecycle_phase", "foreground")
    normalized.setdefault("process_identity", f"{device}:process")
    normalized.setdefault("app_data_container_ref", f"{device}:app-data")
    policy = reset_policy or normalized.get("reset_policy") or "force_stop"
    policy_text = str(policy).strip().casefold()
    if policy_text not in _APP_RESET_POLICIES:
        raise InteractionValidationError(
            "options.reset_policy 仅支持 warm_preferred/force_stop/clear_data/none"
        )
    normalized["reset_policy"] = policy_text
    if "deep_link" in normalized and normalized["deep_link"] is not None:
        link = str(normalized["deep_link"]).strip()
        normalized["deep_link"] = link
    for locale_key in ("language", "region", "experiment"):
        if locale_key in normalized and normalized[locale_key] is not None:
            text = str(normalized[locale_key]).strip()
            if text:
                normalized[locale_key] = text
            else:
                normalized.pop(locale_key, None)
    return normalized


def rotate_app_data_container_ref(
    current: str | None,
    *,
    device_ref: str | None = None,
) -> str:
    """clear_data 后轮转业务数据容器身份，避免同设备误判同会话。"""
    device = str(device_ref or "").strip() or "android:default"
    text = str(current or "").strip()
    if not text:
        return f"{device}:app-data:g1"
    marker = ":app-data:g"
    if marker in text:
        prefix, _, gen_text = text.rpartition(marker)
        try:
            generation = int(gen_text) + 1
        except ValueError:
            generation = 1
        base = prefix if prefix else device
        return f"{base}:app-data:g{generation}"
    if text.endswith(":app-data"):
        return f"{text}:g1"
    return f"{text}:app-data:g1"


def with_rotated_app_data_container(spec: SurfaceSessionSpec) -> SurfaceSessionSpec:
    """返回 clear_data 后容器世代已轮转的新 Session Spec。"""
    options = thaw_mapping(spec.options) if spec.options else {}
    if not isinstance(options, dict):
        options = dict(spec.options or {})
    else:
        options = dict(options)
    options["app_data_container_ref"] = rotate_app_data_container_ref(
        options.get("app_data_container_ref"),
        device_ref=spec.device_ref,
    )
    options["data_cleared"] = True
    return SurfaceSessionSpec(
        task_id=spec.task_id,
        session_id=spec.session_id,
        surface_kind=spec.surface_kind,
        account_environment_ref=spec.account_environment_ref,
        platform_build_ref=spec.platform_build_ref,
        device_ref=spec.device_ref,
        device_lease_id=spec.device_lease_id,
        device_lease_fencing_token=spec.device_lease_fencing_token,
        reset_policy=spec.reset_policy or "clear_data",
        options=options,
    )


@dataclass(frozen=True, kw_only=True)
class SurfaceSessionRef:
    """Driver 已建立会话的公共引用。"""

    task_id: str
    session_id: str
    surface_kind: SurfaceKind
    primary_surface_ref: str

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "primary_surface_ref"):
            require_text(getattr(self, field_name), field_name)
        if not isinstance(self.surface_kind, SurfaceKind):
            raise InteractionValidationError("surface_kind 不受支持")


@dataclass(frozen=True, kw_only=True)
class RawSurfaceObservation:
    """平台原始观测的有界容器；上层需经 Normalizer 后再交给 Agent。"""

    task_id: str
    session_id: str
    surface_kind: SurfaceKind
    surface_ref: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "surface_ref"):
            require_text(getattr(self, field_name), field_name)
        if not isinstance(self.surface_kind, SurfaceKind):
            raise InteractionValidationError("surface_kind 不受支持")
        object.__setattr__(
            self,
            "payload",
            freeze_mapping(
                self.payload,
                "payload",
                forbidden_keys=RUNTIME_HANDLE_KEYS,
            ),
        )


@dataclass(frozen=True, kw_only=True)
class RawActionResult:
    """Driver 对结构化动作的原始执行返回。"""

    action_execution_id: str
    succeeded: bool
    retryable: bool
    payload: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None

    def __post_init__(self) -> None:
        require_text(self.action_execution_id, "action_execution_id")
        if not isinstance(self.succeeded, bool) or not isinstance(self.retryable, bool):
            raise InteractionValidationError("succeeded/retryable 必须为布尔值")
        if self.error_code is not None:
            require_text(self.error_code, "error_code")
        if self.succeeded == (self.error_code is not None):
            raise InteractionValidationError("动作成功状态与 error_code 不一致")
        object.__setattr__(
            self,
            "payload",
            freeze_mapping(
                self.payload,
                "payload",
                forbidden_keys=RUNTIME_HANDLE_KEYS,
            ),
        )


@dataclass(frozen=True, kw_only=True)
class RawArtifact:
    """Driver Capture 阶段返回的未持久化产物。"""

    artifact_type: str
    content: Any

    def __post_init__(self) -> None:
        require_text(self.artifact_type, "artifact_type")
        if isinstance(self.content, (bytes, bytearray)):
            # 截图等二进制产物只存在于 Adapter 进程内边界；复制为不可变 bytes，
            # 跨 Worker 仍必须先由 Capture Writer 落存储并只传 Artifact 引用。
            frozen = bytes(self.content)
        else:
            frozen = freeze_json_mapping({"content": self.content}, "artifact")["content"]
        object.__setattr__(self, "content", frozen)


@dataclass(frozen=True, kw_only=True)
class RawArtifactSet:
    """一次平台 Capture 的原始产物集合。"""

    task_id: str
    session_id: str
    surface_ref: str
    artifacts: tuple[RawArtifact, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "surface_ref"):
            require_text(getattr(self, field_name), field_name)
        artifacts = tuple(self.artifacts)
        if not artifacts or any(not isinstance(item, RawArtifact) for item in artifacts):
            raise InteractionValidationError("artifacts 必须是非空 RawArtifact 集合")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(
            self,
            "metadata",
            freeze_mapping(
                self.metadata,
                "metadata",
                forbidden_keys=RUNTIME_HANDLE_KEYS,
            ),
        )


class SurfaceDriver(Protocol):
    """所有 Web/Android/iOS Adapter 必须实现的统一端口。"""

    async def start_session(self, spec: SurfaceSessionSpec) -> SurfaceSessionRef: ...

    async def reattach_session(self, spec: SurfaceSessionSpec) -> SurfaceSessionRef: ...

    async def observe(
        self,
        *,
        session: SurfaceSessionRef,
        profile: CaptureProfile,
    ) -> RawSurfaceObservation: ...

    async def execute(
        self,
        *,
        session: SurfaceSessionRef,
        intent: ActionIntent,
        action_execution_id: str,
    ) -> RawActionResult: ...

    async def capture(
        self,
        *,
        session: SurfaceSessionRef,
        profile: CaptureProfile,
    ) -> RawArtifactSet: ...

    def describe_capabilities(self) -> SurfaceCapabilityManifest: ...

    async def close_session(self, *, session: SurfaceSessionRef, reason: str) -> None: ...


def public_driver_value(value: Mapping[str, Any]) -> dict[str, Any]:
    """只用于 Adapter 边界测试，证明冻结后的值仍可安全序列化。"""
    return thaw_mapping(freeze_mapping(value, "value", forbidden_keys=RUNTIME_HANDLE_KEYS))
