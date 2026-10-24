"""V2 任务完成、收口与报告绑定合同。"""

from services.api.app.domain.task.completion import (
    COMPLETION_GATE_PROTOCOL_VERSION,
    REPORT_GATE_BINDING_PROTOCOL_VERSION,
    TASK_FINALIZATION_PROTOCOL_VERSION,
    CompletionBlockReason,
    CompletionGateDecision,
    CompletionGateFacts,
    CompletionGateSnapshot,
    ReportGateBinding,
    TaskExecutionStatus,
    TaskFinalizationSnapshot,
    TaskFinalizationState,
    TaskQualityOutcome,
    build_completion_gate_snapshot,
)
from services.api.app.domain.task.execution_mode import (
    TaskExecutionMode,
    lock_task_execution_mode,
    task_execution_mode_from_config,
)
from services.api.app.domain.task.h5_inspection_notification_mode import (
    H5InspectionNotificationMode,
    lock_h5_inspection_notification_mode,
    parse_h5_inspection_notification_mode,
)
from services.api.app.domain.task.platform_build import PlatformBuild, PlatformBuildStatus
from services.api.app.domain.task.status import (
    TASK_STATUS_PROTOCOL_VERSION,
    TASK_STATUS_TRANSITIONS,
    TaskStatus,
    TaskStatusConflictError,
    TaskStatusSnapshot,
    TaskStatusValidationError,
    validate_task_status_transition,
)
from services.api.app.domain.task.submission import (
    TASK_SUBMISSION_COMMITTED_MESSAGE_TYPE,
    TASK_SUBMISSION_WAKEUP_PROTOCOL_VERSION,
    build_task_submission_committed,
)
from services.api.app.domain.task.targets import (
    ADULT_SELF_ATTESTATION_STATEMENT_VERSION,
    AdultSelfAttestation,
    AppTaskConfig,
    TaskTarget,
    TaskTargetPlatform,
    TaskTargetValidationError,
    WebTaskConfig,
)
from services.api.app.domain.task.web_browser_profile import (
    DEFAULT_WEB_BROWSER_PROFILE_ID,
    WebBrowserProfile,
    WebBrowserProfileNotFoundError,
    get_web_browser_profile,
)

__all__ = [
    "COMPLETION_GATE_PROTOCOL_VERSION",
    "REPORT_GATE_BINDING_PROTOCOL_VERSION",
    "TASK_FINALIZATION_PROTOCOL_VERSION",
    "CompletionBlockReason",
    "CompletionGateDecision",
    "CompletionGateFacts",
    "CompletionGateSnapshot",
    "PlatformBuild",
    "PlatformBuildStatus",
    "ReportGateBinding",
    "TaskExecutionStatus",
    "TaskExecutionMode",
    "H5InspectionNotificationMode",
    "TaskFinalizationSnapshot",
    "TaskFinalizationState",
    "TaskQualityOutcome",
    "TASK_STATUS_PROTOCOL_VERSION",
    "TASK_STATUS_TRANSITIONS",
    "TASK_SUBMISSION_COMMITTED_MESSAGE_TYPE",
    "TASK_SUBMISSION_WAKEUP_PROTOCOL_VERSION",
    "ADULT_SELF_ATTESTATION_STATEMENT_VERSION",
    "AdultSelfAttestation",
    "TaskStatus",
    "TaskStatusConflictError",
    "TaskStatusSnapshot",
    "TaskStatusValidationError",
    "AppTaskConfig",
    "WebTaskConfig",
    "TaskTarget",
    "TaskTargetPlatform",
    "TaskTargetValidationError",
    "DEFAULT_WEB_BROWSER_PROFILE_ID",
    "WebBrowserProfile",
    "WebBrowserProfileNotFoundError",
    "build_completion_gate_snapshot",
    "build_task_submission_committed",
    "lock_task_execution_mode",
    "lock_h5_inspection_notification_mode",
    "parse_h5_inspection_notification_mode",
    "task_execution_mode_from_config",
    "get_web_browser_profile",
    "validate_task_status_transition",
]
