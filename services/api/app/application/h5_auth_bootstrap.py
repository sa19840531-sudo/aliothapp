"""H5 正式探索前的主动登录状态机。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Protocol
from urllib.parse import urlsplit

from services.api.app.application.h5_secret_input import (
    H5SecretInputCommand,
    H5SecretInputRuntime,
)
from services.api.app.core.h5_auth_config import (
    H5CredentialProfileRef,
    normalize_h5_auth_hostname,
)
from services.api.app.domain.exploration import (
    H5AuthBootstrapResult,
    H5AuthPageKind,
    H5AuthPageObservation,
    H5AuthStatus,
)
from services.api.app.domain.interaction import CaptureRef, SurfaceKind
from services.api.app.domain.protocol import require_aware_datetime

_MAX_AUTH_PAGE_CHECKS = 20
_MAX_AUTH_NAVIGATIONS = 6
_MAX_AUTH_OBSTRUCTION_TRANSITIONS = 10
_SAFE_POST_SECRET_FAILURE_OBSERVATIONS = frozenset(
    {
        "secret_form_submit_no_request",
        "secret_form_submit_noncredential_post_only",
    }
)


class H5AuthUnsafeSessionError(RuntimeError):
    """秘密字段可能仍留在页面，禁止后续 Capture 或探索。"""


@dataclass(frozen=True, slots=True, kw_only=True)
class H5AuthBootstrapCommand:
    """认证 Bootstrap 所需的最小任务上下文。"""

    task_id: str
    agent_run_id: str
    session_id: str
    target_url: str
    deadline: datetime

    def __post_init__(self) -> None:
        for field_name in ("task_id", "agent_run_id", "session_id", "target_url"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
        parsed = urlsplit(self.target_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("target_url 必须为带 hostname 的 HTTP(S) URL")
        require_aware_datetime(self.deadline, "deadline")

    @property
    def target_hostname(self) -> str:
        hostname = urlsplit(self.target_url).hostname
        assert hostname is not None
        return normalize_h5_auth_hostname(hostname)


@dataclass(frozen=True, slots=True, kw_only=True)
class H5AuthPageSnapshot:
    """一次受信 Capture 的认证分析结果。"""

    capture: CaptureRef
    hostname: str | None
    observation: H5AuthPageObservation

    def __post_init__(self) -> None:
        if not isinstance(self.capture, CaptureRef):
            raise TypeError("capture 必须为 CaptureRef")
        if self.hostname is not None:
            object.__setattr__(
                self,
                "hostname",
                normalize_h5_auth_hostname(self.hostname),
            )
        if not isinstance(self.observation, H5AuthPageObservation):
            raise TypeError("observation 必须为 H5AuthPageObservation")


@dataclass(frozen=True, slots=True, kw_only=True)
class H5AuthNavigationResult:
    """普通认证页面动作的无秘密审计引用。"""

    capture: CaptureRef
    action_execution_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.capture, CaptureRef):
            raise TypeError("capture 必须为 CaptureRef")
        if (
            not self.action_execution_id
            or self.action_execution_id != self.action_execution_id.strip()
        ):
            raise ValueError("action_execution_id 必须为无首尾空白的非空字符串")


@dataclass(frozen=True, slots=True, kw_only=True)
class H5AuthAuditState:
    """Surface Phase 审计适配器返回的恢复游标。"""

    phase_run_id: str | None
    row_version: int
    attempted: bool
    terminal: bool
    registration_attempted: bool = False
    registration_submitted: bool = False
    login_attempted: bool = False
    credential_scope: Literal["domain", "global"] | None = None
    terminal_status: H5AuthStatus | None = None
    terminal_reason_code: str | None = None


class H5CredentialRefResolver(Protocol):
    def resolve_ref(self, hostname: str) -> H5CredentialProfileRef | None: ...


class H5AuthPageReader(Protocol):
    async def read(
        self,
        *,
        command: H5AuthBootstrapCommand,
        capture: CaptureRef,
    ) -> H5AuthPageSnapshot: ...


class H5AuthNavigationPort(Protocol):
    async def click(
        self,
        *,
        command: H5AuthBootstrapCommand,
        source_capture: CaptureRef,
        semantic_target: str,
        step_key: str,
        transition_parameters: Mapping[str, str | int] | None = None,
    ) -> H5AuthNavigationResult: ...

    async def observe(
        self,
        *,
        command: H5AuthBootstrapCommand,
        source_capture: CaptureRef,
        step_key: str,
        transition_parameters: Mapping[str, str | int] | None = None,
    ) -> H5AuthNavigationResult: ...

    async def navigate_root(
        self,
        *,
        command: H5AuthBootstrapCommand,
        source_capture: CaptureRef,
        step_key: str,
    ) -> H5AuthNavigationResult: ...


class H5AuthAuditPort(Protocol):
    async def begin(
        self,
        *,
        command: H5AuthBootstrapCommand,
        source_capture: CaptureRef,
        credential_scope: Literal["domain", "global"] | None,
    ) -> H5AuthAuditState: ...

    async def record(
        self,
        state: H5AuthAuditState,
        *,
        step_key: str,
        status: H5AuthStatus,
        reason_code: str,
        source_capture: CaptureRef,
        result: H5AuthNavigationResult | None = None,
    ) -> H5AuthAuditState: ...

    async def finish(
        self,
        state: H5AuthAuditState,
        *,
        status: H5AuthStatus,
        reason_code: str,
    ) -> None: ...


class H5AuthBootstrapPort(Protocol):
    async def run(
        self,
        command: H5AuthBootstrapCommand,
        *,
        initial_capture: CaptureRef,
    ) -> H5AuthBootstrapResult: ...


class H5AuthBootstrapService:
    """在正式根 Frontier 前执行一次受限、可恢复的主动密码登录。"""

    def __init__(
        self,
        *,
        credential_resolver: H5CredentialRefResolver,
        page_reader: H5AuthPageReader,
        navigator: H5AuthNavigationPort,
        secret_runtime: H5SecretInputRuntime,
        audit: H5AuthAuditPort,
    ) -> None:
        self._credential_resolver = credential_resolver
        self._page_reader = page_reader
        self._navigator = navigator
        self._secret_runtime = secret_runtime
        self._audit = audit

    async def run(
        self,
        command: H5AuthBootstrapCommand,
        *,
        initial_capture: CaptureRef,
    ) -> H5AuthBootstrapResult:
        if not isinstance(command, H5AuthBootstrapCommand):
            raise TypeError("command 必须为 H5AuthBootstrapCommand")
        _validate_capture(command=command, capture=initial_capture)
        credential_ref = self._credential_resolver.resolve_ref(command.target_hostname)
        if credential_ref is None:
            return H5AuthBootstrapResult(
                status=H5AuthStatus.DISABLED,
                final_capture=initial_capture,
                credential_scope=None,
                reason_code="credential_not_configured",
                attempted=False,
                needs_review=False,
            )
        credential_scope = credential_ref.scope if credential_ref is not None else None
        audit_state = await self._audit.begin(
            command=command,
            source_capture=initial_capture,
            credential_scope=credential_scope,
        )
        if audit_state.terminal:
            recovered_status = audit_state.terminal_status or H5AuthStatus.FAILED
            recovered_reason = (
                audit_state.terminal_reason_code or "auth_audit_terminal_state_invalid"
            )
            if recovered_status in {
                H5AuthStatus.ALREADY_AUTHENTICATED,
                H5AuthStatus.AUTHENTICATED,
            }:
                recovered_page = await self._page_reader.read(
                    command=command,
                    capture=initial_capture,
                )
                if (
                    recovered_page.capture.capture_id != initial_capture.capture_id
                    or recovered_page.hostname != command.target_hostname
                    or recovered_page.observation.page_kind
                    is not H5AuthPageKind.PROFILE_AUTHENTICATED
                ):
                    return H5AuthBootstrapResult(
                        status=H5AuthStatus.FAILED,
                        final_capture=initial_capture,
                        credential_scope=audit_state.credential_scope or credential_scope,
                        reason_code="terminal_auth_session_not_verified",
                        attempted=audit_state.attempted,
                        needs_review=True,
                    )
            return H5AuthBootstrapResult(
                status=recovered_status,
                final_capture=initial_capture,
                credential_scope=audit_state.credential_scope or credential_scope,
                reason_code=recovered_reason,
                attempted=audit_state.attempted,
                needs_review=recovered_status in {H5AuthStatus.GUEST_ONLY, H5AuthStatus.FAILED},
            )
        current_capture = initial_capture
        navigation_count = 0
        obstruction_transition_count = 0
        terminal_status = H5AuthStatus.FAILED
        terminal_reason = "auth_page_check_budget_exhausted"

        for _ in range(_MAX_AUTH_PAGE_CHECKS):
            page = await self._page_reader.read(
                command=command,
                capture=current_capture,
            )
            if page.capture.capture_id != current_capture.capture_id:
                raise RuntimeError("认证页面分析结果与输入 Capture 不一致")
            audit_state = await self._audit.record(
                audit_state,
                step_key=f"observe:{current_capture.capture_id}",
                status=H5AuthStatus.CHECKING,
                reason_code=page.observation.reason_code,
                source_capture=current_capture,
            )
            if page.hostname != command.target_hostname:
                terminal_status = H5AuthStatus.FAILED
                terminal_reason = (
                    "auth_page_hostname_unavailable"
                    if page.hostname is None
                    else "auth_page_cross_hostname"
                )
                break

            observation = page.observation
            if observation.page_kind is H5AuthPageKind.PROFILE_AUTHENTICATED:
                terminal_status = (
                    H5AuthStatus.AUTHENTICATED
                    if audit_state.attempted
                    else H5AuthStatus.ALREADY_AUTHENTICATED
                )
                terminal_reason = (
                    "password_login_verified" if audit_state.attempted else "already_authenticated"
                )
                break

            if audit_state.registration_attempted and not audit_state.registration_submitted:
                terminal_status = H5AuthStatus.FAILED
                terminal_reason = "registration_submit_unconfirmed"
                break

            target, purpose, transition_parameters = _ordinary_target(
                observation,
                prefer_registration=(
                    credential_ref is not None
                    and credential_ref.submission_mode == "register_if_available"
                    and not audit_state.attempted
                ),
            )
            if target is not None:
                if observation.page_kind is H5AuthPageKind.TRANSIENT_OBSTRUCTION:
                    if obstruction_transition_count >= _MAX_AUTH_OBSTRUCTION_TRANSITIONS:
                        terminal_status = H5AuthStatus.FAILED
                        terminal_reason = "auth_obstruction_budget_exhausted"
                        break
                    obstruction_transition_count += 1
                    step_key = (
                        f"obstruction:{obstruction_transition_count}:{current_capture.capture_id}"
                    )
                else:
                    if navigation_count >= _MAX_AUTH_NAVIGATIONS:
                        terminal_status = H5AuthStatus.FAILED
                        terminal_reason = "auth_navigation_budget_exhausted"
                        break
                    navigation_count += 1
                    step_key = f"navigate:{navigation_count}:{current_capture.capture_id}"
                click_arguments = {
                    "command": command,
                    "source_capture": current_capture,
                    "semantic_target": target,
                    "step_key": step_key,
                }
                if transition_parameters:
                    click_arguments["transition_parameters"] = transition_parameters
                navigation = await self._navigator.click(**click_arguments)
                audit_state = await self._audit.record(
                    audit_state,
                    step_key=step_key,
                    status=H5AuthStatus.CHECKING,
                    reason_code=purpose,
                    source_capture=current_capture,
                    result=navigation,
                )
                current_capture = navigation.capture
                continue

            if observation.page_kind is H5AuthPageKind.TRANSIENT_OBSTRUCTION:
                if obstruction_transition_count >= _MAX_AUTH_OBSTRUCTION_TRANSITIONS:
                    terminal_status = H5AuthStatus.FAILED
                    terminal_reason = "auth_obstruction_budget_exhausted"
                    break
                obstruction_transition_count += 1
                step_key = (
                    "obstruction_observe:"
                    f"{obstruction_transition_count}:{current_capture.capture_id}"
                )
                observe_arguments = {
                    "command": command,
                    "source_capture": current_capture,
                    "step_key": step_key,
                }
                if transition_parameters:
                    observe_arguments["transition_parameters"] = transition_parameters
                navigation = await self._navigator.observe(**observe_arguments)
                audit_state = await self._audit.record(
                    audit_state,
                    step_key=step_key,
                    status=H5AuthStatus.CHECKING,
                    reason_code=purpose,
                    source_capture=current_capture,
                    result=navigation,
                )
                current_capture = navigation.capture
                continue

            if observation.page_kind is H5AuthPageKind.LOGIN_CHALLENGE:
                terminal_status = H5AuthStatus.GUEST_ONLY
                terminal_reason = "unsupported_login_challenge"
                break

            if observation.page_kind is H5AuthPageKind.PROFILE_GUEST:
                terminal_status = H5AuthStatus.GUEST_ONLY
                terminal_reason = (
                    "credential_not_configured"
                    if credential_ref is None
                    else "profile_login_entry_unavailable"
                )
                break

            if observation.page_kind is H5AuthPageKind.LOGIN_PASSWORD:
                if credential_ref is None:
                    terminal_status = H5AuthStatus.GUEST_ONLY
                    terminal_reason = "credential_not_configured"
                    break
                if audit_state.registration_submitted and credential_ref.uses_synthetic_phone:
                    terminal_status = H5AuthStatus.GUEST_ONLY
                    terminal_reason = "registration_requires_stable_credential_for_login"
                    break
                submit_target, submit_kind = _credential_submit_plan(
                    observation=observation,
                    credential_ref=credential_ref,
                    registration_submitted=audit_state.registration_submitted,
                )
                if (
                    audit_state.login_attempted
                    or (audit_state.attempted and not audit_state.registration_attempted)
                    or (submit_kind == "register" and audit_state.registration_attempted)
                ):
                    terminal_status = H5AuthStatus.FAILED
                    terminal_reason = "credential_submit_already_recorded"
                    break
                if not _password_targets_complete(
                    observation,
                    submit_target=submit_target,
                ):
                    terminal_status = H5AuthStatus.FAILED
                    terminal_reason = "password_form_incomplete"
                    break
                planned_step = (
                    "registration_submit_planned"
                    if submit_kind == "register"
                    else "credential_submit_planned"
                )
                result_step = (
                    "registration_submit_result"
                    if submit_kind == "register"
                    else "credential_submit_result"
                )
                audit_state = await self._audit.record(
                    audit_state,
                    step_key=planned_step,
                    status=H5AuthStatus.CHECKING,
                    reason_code=planned_step,
                    source_capture=current_capture,
                )
                audit_state = replace(
                    audit_state,
                    attempted=True,
                    registration_attempted=(
                        audit_state.registration_attempted or submit_kind == "register"
                    ),
                    login_attempted=(audit_state.login_attempted or submit_kind == "login"),
                )
                secret_result = await self._secret_runtime.execute(
                    H5SecretInputCommand(
                        task_id=command.task_id,
                        agent_run_id=command.agent_run_id,
                        session_id=command.session_id,
                        source_capture_id=current_capture.capture_id,
                        target_hostname=command.target_hostname,
                        credential_ref=credential_ref,
                        phone_semantic_target=observation.phone_target or "",
                        password_semantic_target=observation.password_target or "",
                        submit_semantic_target=submit_target or "",
                        deadline=command.deadline,
                    )
                )
                if secret_result.after_capture is not None:
                    _validate_capture(command=command, capture=secret_result.after_capture)
                audit_state = await self._audit.record(
                    audit_state,
                    step_key=result_step,
                    status=(
                        H5AuthStatus.CHECKING
                        if secret_result.submitted and secret_result.fields_scrubbed
                        else H5AuthStatus.FAILED
                    ),
                    reason_code=secret_result.reason_code,
                    source_capture=current_capture,
                    result=(
                        H5AuthNavigationResult(
                            capture=secret_result.after_capture,
                            action_execution_id=secret_result.action_execution_id,
                        )
                        if secret_result.after_capture is not None
                        and secret_result.action_execution_id is not None
                        else None
                    ),
                )
                if secret_result.after_capture is not None:
                    current_capture = secret_result.after_capture
                if (
                    submit_kind == "register"
                    and secret_result.submitted
                    and secret_result.fields_scrubbed
                ):
                    audit_state = replace(audit_state, registration_submitted=True)
                if not secret_result.fields_scrubbed:
                    await self._audit.finish(
                        audit_state,
                        status=H5AuthStatus.FAILED,
                        reason_code="secret_fields_scrub_failed",
                    )
                    raise H5AuthUnsafeSessionError("H5 登录秘密字段无法确认已清空，禁止继续采集")
                if secret_result.hostname != command.target_hostname:
                    terminal_status = H5AuthStatus.FAILED
                    terminal_reason = "secret_submit_cross_hostname"
                    break
                if not secret_result.submitted:
                    if secret_result.reason_code in _SAFE_POST_SECRET_FAILURE_OBSERVATIONS:
                        observation_step = (
                            f"post_submit_failure_observe:{current_capture.capture_id}"
                        )
                        navigation = await self._navigator.observe(
                            command=command,
                            source_capture=current_capture,
                            step_key=observation_step,
                        )
                        audit_state = await self._audit.record(
                            audit_state,
                            step_key=observation_step,
                            status=H5AuthStatus.FAILED,
                            reason_code=secret_result.reason_code,
                            source_capture=current_capture,
                            result=navigation,
                        )
                        current_capture = navigation.capture
                    terminal_status = H5AuthStatus.FAILED
                    terminal_reason = secret_result.reason_code
                    break
                observation_step = f"post_submit_observe:{current_capture.capture_id}"
                navigation = await self._navigator.observe(
                    command=command,
                    source_capture=current_capture,
                    step_key=observation_step,
                )
                audit_state = await self._audit.record(
                    audit_state,
                    step_key=observation_step,
                    status=H5AuthStatus.CHECKING,
                    reason_code="credential_submitted_reobserve",
                    source_capture=current_capture,
                    result=navigation,
                )
                current_capture = navigation.capture
                continue

            terminal_status = (
                H5AuthStatus.FAILED if audit_state.attempted else H5AuthStatus.GUEST_ONLY
            )
            terminal_reason = (
                "login_not_confirmed" if audit_state.attempted else observation.reason_code
            )
            break

        root_navigation = await self._navigator.navigate_root(
            command=command,
            source_capture=current_capture,
            step_key="return_to_task_root",
        )
        audit_state = await self._audit.record(
            audit_state,
            step_key="return_to_task_root",
            status=terminal_status,
            reason_code=terminal_reason,
            source_capture=current_capture,
            result=root_navigation,
        )
        await self._audit.finish(
            audit_state,
            status=terminal_status,
            reason_code=terminal_reason,
        )
        return H5AuthBootstrapResult(
            status=terminal_status,
            final_capture=root_navigation.capture,
            credential_scope=credential_scope,
            reason_code=terminal_reason,
            attempted=audit_state.attempted,
            needs_review=terminal_status in {H5AuthStatus.GUEST_ONLY, H5AuthStatus.FAILED},
        )


class NoopH5AuthBootstrap:
    """开关关闭时保持原 H5 首采行为。"""

    async def run(
        self,
        command: H5AuthBootstrapCommand,
        *,
        initial_capture: CaptureRef,
    ) -> H5AuthBootstrapResult:
        _validate_capture(command=command, capture=initial_capture)
        return H5AuthBootstrapResult(
            status=H5AuthStatus.DISABLED,
            final_capture=initial_capture,
            credential_scope=None,
            reason_code="h5_auth_disabled",
            attempted=False,
            needs_review=False,
        )


def _ordinary_target(
    observation: H5AuthPageObservation,
    *,
    prefer_registration: bool = False,
) -> tuple[str | None, str, dict[str, str | int]]:
    transition_parameters = dict(observation.transition_parameters)
    if observation.page_kind is H5AuthPageKind.TRANSIENT_OBSTRUCTION:
        return (
            observation.transient_target,
            observation.reason_code,
            transition_parameters,
        )
    if observation.profile_target is not None:
        return observation.profile_target, "open_profile_entry", transition_parameters
    if prefer_registration and observation.registration_entry_target is not None:
        return observation.registration_entry_target, "open_registration_entry", {}
    if observation.page_kind is H5AuthPageKind.PROFILE_GUEST:
        return (
            observation.login_target,
            "open_login_entry",
            transition_parameters,
        )
    if observation.page_kind is H5AuthPageKind.LOGIN_SWITCHABLE:
        return observation.password_mode_target, "switch_to_password_mode", {}
    if observation.page_kind is H5AuthPageKind.SUCCESS_NOTICE:
        return (
            observation.success_dismiss_target,
            "dismiss_registration_success_notice",
            transition_parameters,
        )
    return None, "no_ordinary_auth_target", {}


def _credential_submit_plan(
    *,
    observation: H5AuthPageObservation,
    credential_ref: H5CredentialProfileRef,
    registration_submitted: bool,
) -> tuple[str | None, Literal["register", "login"]]:
    registration_target = observation.registration_submit_target
    if registration_target is None and observation.submit_target is not None:
        label = observation.submit_target.casefold()
        if any(marker in label for marker in ("注册", "register", "sign up", "signup")):
            registration_target = observation.submit_target
    if (
        credential_ref.submission_mode == "register_if_available"
        and not registration_submitted
        and registration_target is not None
    ):
        return registration_target, "register"
    if observation.submit_target == registration_target:
        return None, "login"
    return observation.submit_target, "login"


def _password_targets_complete(
    observation: H5AuthPageObservation,
    *,
    submit_target: str | None,
) -> bool:
    return all(
        (
            observation.phone_target,
            observation.password_target,
            submit_target,
        )
    )


def _validate_capture(
    *,
    command: H5AuthBootstrapCommand,
    capture: CaptureRef,
) -> None:
    if not isinstance(capture, CaptureRef):
        raise TypeError("initial_capture 必须为 CaptureRef")
    if (
        capture.task_id != command.task_id
        or capture.session_id != command.session_id
        or capture.surface_kind is not SurfaceKind.WEB
    ):
        raise ValueError("initial_capture 与 H5 认证任务会话不一致")


__all__ = [
    "H5AuthAuditPort",
    "H5AuthAuditState",
    "H5AuthBootstrapCommand",
    "H5AuthBootstrapPort",
    "H5AuthBootstrapService",
    "H5AuthNavigationResult",
    "H5AuthPageSnapshot",
    "H5AuthUnsafeSessionError",
    "NoopH5AuthBootstrap",
]
