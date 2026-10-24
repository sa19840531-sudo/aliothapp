"""H5 密码登录专用秘密输入端口；命令与结果均不携带明文。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from services.api.app.core.h5_auth_config import (
    H5CredentialProfileRef,
    normalize_h5_auth_hostname,
)
from services.api.app.domain.interaction import CaptureRef
from services.api.app.domain.protocol import require_aware_datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class H5SecretInputCommand:
    """专用 Runtime 的安全命令，凭证只以不透明引用出现。"""

    task_id: str
    session_id: str
    source_capture_id: str
    target_hostname: str
    credential_ref: H5CredentialProfileRef
    phone_semantic_target: str
    password_semantic_target: str
    submit_semantic_target: str
    deadline: datetime
    agent_run_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "session_id",
            "source_capture_id",
            "phone_semantic_target",
            "password_semantic_target",
            "submit_semantic_target",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
        object.__setattr__(
            self,
            "target_hostname",
            normalize_h5_auth_hostname(self.target_hostname),
        )
        if not isinstance(self.credential_ref, H5CredentialProfileRef):
            raise TypeError("credential_ref 必须为 H5CredentialProfileRef")
        if self.credential_ref.hostname != self.target_hostname:
            raise ValueError("credential_ref 与任务目标 hostname 不一致")
        require_aware_datetime(self.deadline, "deadline")
        if self.agent_run_id is not None and (
            not self.agent_run_id or self.agent_run_id != self.agent_run_id.strip()
        ):
            raise ValueError("agent_run_id 必须为无首尾空白的非空字符串")

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "source_capture_id": self.source_capture_id,
            "agent_run_id": self.agent_run_id,
            "target_hostname": self.target_hostname,
            "credential_ref": self.credential_ref.to_safe_dict(),
            "deadline": self.deadline.isoformat(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class H5SecretInputResult:
    """秘密输入窗口的无秘密结果。"""

    submitted: bool
    fields_scrubbed: bool
    hostname: str
    reason_code: str
    after_capture: CaptureRef | None = None
    action_execution_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "hostname", normalize_h5_auth_hostname(self.hostname))
        if not self.reason_code or self.reason_code != self.reason_code.strip():
            raise ValueError("reason_code 必须为无首尾空白的非空字符串")
        if (self.after_capture is None) != (self.action_execution_id is None):
            raise ValueError("秘密输入后态与动作身份必须同时提供")
        if self.after_capture is not None:
            if not isinstance(self.after_capture, CaptureRef):
                raise TypeError("after_capture 必须为 CaptureRef")
            if self.after_capture.action_execution_id != self.action_execution_id:
                raise ValueError("秘密输入后态与动作身份不一致")
            if not self.fields_scrubbed:
                raise ValueError("秘密字段未清空时不允许返回后态 Capture")

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "submitted": self.submitted,
            "fields_scrubbed": self.fields_scrubbed,
            "hostname": self.hostname,
            "reason_code": self.reason_code,
            "after_capture_id": self.after_capture.capture_id if self.after_capture else None,
            "action_execution_id": self.action_execution_id,
        }


class H5SecretInputRuntime(Protocol):
    """H5 Auth Bootstrap 唯一可调用的密码填写端口。"""

    async def execute(self, command: H5SecretInputCommand) -> H5SecretInputResult: ...


__all__ = [
    "H5SecretInputCommand",
    "H5SecretInputResult",
    "H5SecretInputRuntime",
]
