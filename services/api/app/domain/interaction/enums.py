"""跨端交互合同共用的枚举。"""

from enum import StrEnum


class SurfaceKind(StrEnum):
    """Interaction Runtime 可适配的界面类型。"""

    WEB = "web"
    ANDROID = "android"
    IOS = "ios"


class InteractionOperation(StrEnum):
    """Journey 与 Explorer 共用的结构化动作类型。"""

    NAVIGATE = "navigate"
    CLICK = "click"
    TAP = "tap"
    INPUT = "input"
    HOVER = "hover"
    SCROLL = "scroll"
    BACK = "back"
    REFRESH = "refresh"
    OBSERVE = "observe"
    TIMED_CAPTURE = "timed_capture"
    SEEK_MEDIA = "seek_media"
    NETWORK_FAULT = "network_fault"
    DEEP_LINK = "deep_link"
    WARM_RESTORE = "warm_restore"
    RESET_APP = "reset_app"
    SET_CLIPBOARD = "set_clipboard"
    OPEN_EXTERNAL = "open_external"


class InteractionRiskLevel(StrEnum):
    """供确定性风险策略二次校验的风险提示。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class InteractionResultStatus(StrEnum):
    """Interaction Runtime 对一次动作返回的可路由状态。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    TIMED_OUT = "timed_out"
    EVIDENCE_PENDING = "evidence_pending"
    EVIDENCE_FAILED = "evidence_failed"


class InteractionExecutionOutcome(StrEnum):
    """设备动作本身的执行事实。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class InteractionVerificationOutcome(StrEnum):
    """动作效果相对于预期的独立验证事实。"""

    PENDING = "pending"
    EFFECT_CONFIRMED = "effect_confirmed"
    NO_EFFECT = "no_effect"
    EFFECT_UNEXPECTED = "effect_unexpected"
    UNVERIFIED = "unverified"
