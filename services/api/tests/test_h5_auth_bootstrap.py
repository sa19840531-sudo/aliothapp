"""H5 主动登录状态机、一次提交和降级终态回归。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.capture_coordinator import InProcessSessionCommandGate
from services.api.app.application.h5_auth_audit import SurfacePhaseH5AuthAudit
from services.api.app.application.h5_auth_bootstrap import (
    H5AuthAuditState,
    H5AuthBootstrapCommand,
    H5AuthBootstrapService,
    H5AuthNavigationResult,
    H5AuthPageSnapshot,
    H5AuthUnsafeSessionError,
)
from services.api.app.application.h5_auth_navigation import CoordinatedH5AuthNavigation
from services.api.app.application.h5_secret_input import H5SecretInputResult
from services.api.app.core.h5_auth_config import H5CredentialProfileRef
from services.api.app.domain.exploration import (
    H5AuthPageKind,
    H5AuthPageObservation,
    H5AuthStatus,
    SurfacePhaseRunSnapshot,
    SurfacePhaseRunStatus,
    SurfacePhaseStepSnapshot,
    SurfacePhaseStepStatus,
    build_surface_phase_run_id,
)
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    SurfaceKind,
)


def _capture(capture_id: str) -> CaptureRef:
    return CaptureRef(
        task_id="task-auth",
        session_id="session-auth",
        capture_id=capture_id,
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
    )


def _observation(
    page_kind: H5AuthPageKind,
    reason_code: str,
    **targets: object,
) -> H5AuthPageObservation:
    return H5AuthPageObservation(
        page_kind=page_kind,
        reason_code=reason_code,
        **targets,
    )


def _page(capture_id: str, observation: H5AuthPageObservation, *, hostname: str = "h5.example.com"):
    return H5AuthPageSnapshot(
        capture=_capture(capture_id),
        hostname=hostname,
        observation=observation,
    )


def _command() -> H5AuthBootstrapCommand:
    return H5AuthBootstrapCommand(
        task_id="task-auth",
        agent_run_id="run-auth",
        session_id="session-auth",
        target_url="https://h5.example.com/root",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )


@pytest.mark.asyncio
async def test_auth_navigation_keeps_h5_mobile_viewport() -> None:
    commands = []

    class Coordinator:
        async def coordinate(self, command):
            commands.append(command)
            return type(
                "Coordination",
                (),
                {
                    "after_capture": _capture("capture-root"),
                    "interaction": type(
                        "Interaction",
                        (),
                        {"action_execution_id": "action-auth-root"},
                    )(),
                },
            )()

    result = await CoordinatedH5AuthNavigation(
        coordinator=Coordinator(),
    ).navigate_root(
        command=_command(),
        source_capture=_capture("capture-login"),
        step_key="return-root",
    )

    parameters = commands[0].intent.parameters
    assert parameters["destination_url"] == "https://h5.example.com/root"
    assert parameters["viewport_width"] == 390
    assert parameters["viewport_height"] == 844
    assert result.action_execution_id == "action-auth-root"


@pytest.mark.asyncio
async def test_auth_navigation_preserves_controlled_obstruction_parameters() -> None:
    commands = []

    class Coordinator:
        async def coordinate(self, command):
            commands.append(command)
            return type(
                "Coordination",
                (),
                {
                    "after_capture": _capture("capture-after-popup"),
                    "interaction": type(
                        "Interaction",
                        (),
                        {"action_execution_id": "action-popup"},
                    )(),
                },
            )()

    await CoordinatedH5AuthNavigation(coordinator=Coordinator()).click(
        command=_command(),
        source_capture=_capture("capture-popup"),
        semantic_target="关闭当前弹窗",
        step_key="clear-popup",
        transition_parameters={
            "target_mode": "popup_dismissal",
            "popup_dismissal_capability": "flutter_centered_ad_popup_close",
            "obstruction_signature": "h5-obstruction:" + "a" * 64,
        },
    )

    parameters = commands[0].intent.parameters
    assert parameters["target_mode"] == "popup_dismissal"
    assert parameters["popup_dismissal_capability"] == (
        "flutter_centered_ad_popup_close"
    )
    assert parameters["obstruction_signature"] == "h5-obstruction:" + "a" * 64
    assert parameters["viewport_width"] == 390


class _Credentials:
    def __init__(
        self,
        *,
        available: bool = True,
        submission_mode: str = "login",
        uses_synthetic_phone: bool = False,
    ) -> None:
        self.available = available
        self.submission_mode = submission_mode
        self.uses_synthetic_phone = uses_synthetic_phone

    def resolve_ref(self, hostname: str):
        assert hostname == "h5.example.com"
        if not self.available:
            return None
        return H5CredentialProfileRef(
            scope="global",
            hostname=hostname,
            submission_mode=self.submission_mode,
            uses_synthetic_phone=self.uses_synthetic_phone,
        )


class _PageReader:
    def __init__(self, pages: dict[str, H5AuthPageSnapshot]) -> None:
        self.pages = pages
        self.reads: list[str] = []

    async def read(self, *, command, capture):
        assert command.task_id == "task-auth"
        assert command.session_id == "session-auth"
        self.reads.append(capture.capture_id)
        return self.pages[capture.capture_id]


class _Navigator:
    def __init__(
        self,
        *,
        click_captures: list[str] | None = None,
        observe_captures: list[str] | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.click_captures = list(click_captures or [])
        self.observe_captures = list(observe_captures or [])
        self.events = events if events is not None else []
        self.calls: list[dict[str, str]] = []

    async def click(
        self,
        *,
        command,
        source_capture,
        semantic_target,
        step_key,
        transition_parameters=None,
    ):
        del command
        capture_id = self.click_captures.pop(0)
        self.events.append(f"click:{semantic_target}")
        self.calls.append(
            {
                "operation": "click",
                "source": source_capture.capture_id,
                "target": semantic_target,
                "step_key": step_key,
                "transition_parameters": dict(transition_parameters or {}),
            }
        )
        return H5AuthNavigationResult(
            capture=_capture(capture_id),
            action_execution_id=f"action:{step_key}",
        )

    async def observe(
        self,
        *,
        command,
        source_capture,
        step_key,
        transition_parameters=None,
    ):
        del command
        capture_id = self.observe_captures.pop(0)
        self.events.append("observe")
        self.calls.append(
            {
                "operation": "observe",
                "source": source_capture.capture_id,
                "target": "",
                "step_key": step_key,
                "transition_parameters": dict(transition_parameters or {}),
            }
        )
        return H5AuthNavigationResult(
            capture=_capture(capture_id),
            action_execution_id=f"action:{step_key}",
        )

    async def navigate_root(self, *, command, source_capture, step_key):
        del command
        self.events.append("navigate_root")
        self.calls.append(
            {
                "operation": "navigate_root",
                "source": source_capture.capture_id,
                "target": "root",
                "step_key": step_key,
            }
        )
        return H5AuthNavigationResult(
            capture=_capture("capture-root"),
            action_execution_id=f"action:{step_key}",
        )


class _SecretRuntime:
    def __init__(
        self,
        result: H5SecretInputResult,
        *,
        events: list[str] | None = None,
    ) -> None:
        self.result = result
        self.events = events if events is not None else []
        self.calls = 0
        self.commands = []

    async def execute(self, command):
        self.calls += 1
        self.commands.append(command)
        self.events.append("secret_submit")
        assert command.target_hostname == "h5.example.com"
        assert command.credential_ref.scope == "global"
        return self.result


@pytest.mark.asyncio
async def test_registration_link_then_login_uses_two_bounded_submissions() -> None:
    navigator = _Navigator(click_captures=["capture-register"], observe_captures=[
        "capture-login-after-register", "capture-authenticated",
    ])
    secret = _SecretRuntime(H5SecretInputResult(
        submitted=True, fields_scrubbed=True, hostname="h5.example.com",
        reason_code="secret_form_submitted",
    ))
    audit = _Audit()
    service = _service(
        pages={
            "capture-initial": _page("capture-initial", _observation(
                H5AuthPageKind.LOGIN_PASSWORD, "password_form_ready",
                phone_target="手机号", password_target="密码", submit_target="登录",
                registration_entry_target="注册",
            )),
            "capture-register": _page("capture-register", _observation(
                H5AuthPageKind.LOGIN_PASSWORD, "password_form_ready",
                phone_target="手机号", password_target="密码", submit_target="注册",
                registration_submit_target="注册",
            )),
            "capture-login-after-register": _page("capture-login-after-register", _observation(
                H5AuthPageKind.LOGIN_PASSWORD, "password_form_ready",
                phone_target="手机号", password_target="密码", submit_target="登录",
            )),
            "capture-authenticated": _page("capture-authenticated", _observation(
                H5AuthPageKind.PROFILE_AUTHENTICATED, "authenticated_marker_visible",
            )),
        },
        navigator=navigator,
        secret=secret,
        audit=audit,
        submission_mode="register_if_available",
    )
    result = await service.run(_command(), initial_capture=_capture("capture-initial"))
    assert result.status is H5AuthStatus.AUTHENTICATED
    assert secret.calls == 2
    assert [item.submit_semantic_target for item in secret.commands] == ["注册", "登录"]
    assert [call["target"] for call in navigator.calls] == ["注册", "", "", "root"]


@pytest.mark.asyncio
async def test_login_only_configuration_never_submits_registration_form() -> None:
    navigator = _Navigator()
    secret = _SecretRuntime(H5SecretInputResult(
        submitted=True, fields_scrubbed=True, hostname="h5.example.com",
        reason_code="unused",
    ))
    service = _service(
        pages={"capture-initial": _page("capture-initial", _observation(
            H5AuthPageKind.LOGIN_PASSWORD, "password_form_ready",
            phone_target="手机号", password_target="密码", submit_target="注册",
            registration_submit_target="注册",
        ))},
        navigator=navigator,
        secret=secret,
        audit=_Audit(),
    )
    result = await service.run(_command(), initial_capture=_capture("capture-initial"))
    assert result.status is H5AuthStatus.FAILED
    assert secret.calls == 0


@pytest.mark.asyncio
async def test_login_only_configuration_rejects_legacy_registration_submit_target() -> None:
    secret = _SecretRuntime(H5SecretInputResult(
        submitted=True, fields_scrubbed=True, hostname="h5.example.com",
        reason_code="unused",
    ))
    service = _service(
        pages={"capture-initial": _page("capture-initial", _observation(
            H5AuthPageKind.LOGIN_PASSWORD, "password_form_ready",
            phone_target="手机号", password_target="密码", submit_target="注册",
        ))},
        navigator=_Navigator(),
        secret=secret,
        audit=_Audit(),
    )
    result = await service.run(_command(), initial_capture=_capture("capture-initial"))
    assert result.status is H5AuthStatus.FAILED
    assert result.reason_code == "password_form_incomplete"
    assert secret.calls == 0


@pytest.mark.asyncio
async def test_synthetic_registration_does_not_login_with_a_different_phone() -> None:
    navigator = _Navigator(observe_captures=["capture-login"])
    secret = _SecretRuntime(H5SecretInputResult(
        submitted=True, fields_scrubbed=True, hostname="h5.example.com",
        reason_code="secret_form_submitted",
    ))
    service = _service(
        pages={
            "capture-initial": _page("capture-initial", _observation(
                H5AuthPageKind.LOGIN_PASSWORD, "password_form_ready",
                phone_target="手机号", password_target="密码", submit_target="注册",
                registration_submit_target="注册",
            )),
            "capture-login": _page("capture-login", _observation(
                H5AuthPageKind.LOGIN_PASSWORD, "password_form_ready",
                phone_target="手机号", password_target="密码", submit_target="登录",
            )),
        },
        navigator=navigator,
        secret=secret,
        audit=_Audit(),
        submission_mode="register_if_available",
        uses_synthetic_phone=True,
    )
    result = await service.run(_command(), initial_capture=_capture("capture-initial"))
    assert result.status is H5AuthStatus.GUEST_ONLY
    assert result.reason_code == "registration_requires_stable_credential_for_login"
    assert secret.calls == 1


@pytest.mark.asyncio
async def test_recovered_registration_planned_without_result_never_resubmits() -> None:
    navigator = _Navigator()
    secret = _SecretRuntime(H5SecretInputResult(
        submitted=True, fields_scrubbed=True, hostname="h5.example.com",
        reason_code="unused",
    ))
    service = _service(
        pages={"capture-initial": _page("capture-initial", _observation(
            H5AuthPageKind.LOGIN_PASSWORD, "password_form_ready",
            phone_target="手机号", password_target="密码", submit_target="注册",
            registration_submit_target="注册",
        ))},
        navigator=navigator,
        secret=secret,
        audit=_Audit(attempted=True, registration_attempted=True),
        submission_mode="register_if_available",
    )
    result = await service.run(_command(), initial_capture=_capture("capture-initial"))
    assert result.reason_code == "registration_submit_unconfirmed"
    assert secret.calls == 0


class _Audit:
    def __init__(
        self,
        *,
        attempted: bool = False,
        registration_attempted: bool = False,
        events: list[str] | None = None,
    ) -> None:
        self.attempted = attempted
        self.registration_attempted = registration_attempted
        self.events = events if events is not None else []
        self.records: list[dict[str, object]] = []
        self.finished: tuple[H5AuthStatus, str] | None = None

    async def begin(self, *, command, source_capture, credential_scope):
        del command, source_capture, credential_scope
        return H5AuthAuditState(
            phase_run_id="phase-auth",
            row_version=0,
            attempted=self.attempted,
            registration_attempted=self.registration_attempted,
            terminal=False,
        )

    async def record(
        self,
        state,
        *,
        step_key,
        status,
        reason_code,
        source_capture,
        result=None,
    ):
        self.events.append(f"audit:{step_key}")
        self.records.append(
            {
                "step_key": step_key,
                "status": status.value,
                "reason_code": reason_code,
                "source_capture_id": source_capture.capture_id,
                "result_capture_id": result.capture.capture_id if result else None,
                "action_execution_id": result.action_execution_id if result else None,
            }
        )
        return replace(
            state,
            row_version=state.row_version + 1,
            attempted=state.attempted or step_key == "credential_submit_planned",
        )

    async def finish(self, state, *, status, reason_code):
        del state
        self.finished = (status, reason_code)


def _service(
    *,
    pages: dict[str, H5AuthPageSnapshot],
    navigator: _Navigator,
    secret: _SecretRuntime,
    audit: _Audit,
    credentials_available: bool = True,
    submission_mode: str = "login",
    uses_synthetic_phone: bool = False,
) -> H5AuthBootstrapService:
    return H5AuthBootstrapService(
        credential_resolver=_Credentials(
            available=credentials_available,
            submission_mode=submission_mode,
            uses_synthetic_phone=uses_synthetic_phone,
        ),
        page_reader=_PageReader(pages),
        navigator=navigator,
        secret_runtime=secret,
        audit=audit,
    )


@pytest.mark.asyncio
async def test_unconfigured_hostname_skips_auth_probe_without_blocking_exploration() -> None:
    reader = _PageReader({})
    navigator = _Navigator()
    secret = _SecretRuntime(
        H5SecretInputResult(
            submitted=True,
            fields_scrubbed=True,
            hostname="h5.example.com",
            reason_code="unused",
        )
    )
    audit = _Audit()
    service = H5AuthBootstrapService(
        credential_resolver=_Credentials(available=False),
        page_reader=reader,
        navigator=navigator,
        secret_runtime=secret,
        audit=audit,
    )

    result = await service.run(
        _command(),
        initial_capture=_capture("capture-initial"),
    )

    assert result.status is H5AuthStatus.DISABLED
    assert result.final_capture.capture_id == "capture-initial"
    assert result.credential_scope is None
    assert result.reason_code == "credential_not_configured"
    assert result.attempted is False
    assert result.needs_review is False
    assert reader.reads == []
    assert navigator.calls == []
    assert secret.calls == 0
    assert audit.records == []
    assert audit.finished is None


@pytest.mark.asyncio
async def test_pre_auth_popup_and_startup_ad_are_cleared_before_profile_probe() -> None:
    navigator = _Navigator(
        click_captures=["capture-countdown", "capture-home", "capture-profile"],
        observe_captures=["capture-skip"],
    )
    secret = _SecretRuntime(
        H5SecretInputResult(
            submitted=True,
            fields_scrubbed=True,
            hostname="h5.example.com",
            reason_code="unused",
        )
    )
    audit = _Audit()
    popup_parameters = (
        ("target_mode", "popup_dismissal"),
        ("popup_dismissal_capability", "flutter_centered_ad_popup_close"),
        ("obstruction_signature", "h5-obstruction:" + "a" * 64),
    )
    service = _service(
        pages={
            "capture-initial": _page(
                "capture-initial",
                _observation(
                    H5AuthPageKind.TRANSIENT_OBSTRUCTION,
                    "pre_auth_popup_visible",
                    transient_target="关闭当前弹窗",
                    transition_parameters=popup_parameters,
                ),
            ),
            "capture-countdown": _page(
                "capture-countdown",
                _observation(
                    H5AuthPageKind.TRANSIENT_OBSTRUCTION,
                    "startup_ad_countdown_wait",
                    transition_parameters=(
                        ("target_mode", "startup_ad_countdown_reobserve"),
                    ),
                ),
            ),
            "capture-skip": _page(
                "capture-skip",
                _observation(
                    H5AuthPageKind.TRANSIENT_OBSTRUCTION,
                    "startup_ad_skip_visible",
                    transient_target="跳过",
                    transition_parameters=(
                        ("target_mode", "startup_ad_dismissal"),
                        ("startup_ad_capability", "countdown_close"),
                    ),
                ),
            ),
            "capture-home": _page(
                "capture-home",
                _observation(
                    H5AuthPageKind.UNKNOWN,
                    "profile_entry_available",
                    profile_target="我的",
                ),
            ),
            "capture-profile": _page(
                "capture-profile",
                _observation(
                    H5AuthPageKind.PROFILE_AUTHENTICATED,
                    "authenticated_marker_visible",
                ),
            ),
        },
        navigator=navigator,
        secret=secret,
        audit=audit,
    )

    result = await service.run(_command(), initial_capture=_capture("capture-initial"))

    assert result.status is H5AuthStatus.ALREADY_AUTHENTICATED
    assert [call["operation"] for call in navigator.calls] == [
        "click",
        "observe",
        "click",
        "click",
        "navigate_root",
    ]
    assert navigator.calls[0]["transition_parameters"] == dict(popup_parameters)
    assert navigator.calls[1]["transition_parameters"] == {
        "target_mode": "startup_ad_countdown_reobserve"
    }
    assert navigator.calls[2]["transition_parameters"] == {
        "target_mode": "startup_ad_dismissal",
        "startup_ad_capability": "countdown_close",
    }


@pytest.mark.asyncio
async def test_already_authenticated_profile_skips_secret_and_returns_root() -> None:
    secret = _SecretRuntime(
        H5SecretInputResult(
            submitted=True,
            fields_scrubbed=True,
            hostname="h5.example.com",
            reason_code="secret_form_submitted",
        )
    )
    navigator = _Navigator()
    audit = _Audit()
    service = _service(
        pages={
            "capture-initial": _page(
                "capture-initial",
                _observation(
                    H5AuthPageKind.PROFILE_AUTHENTICATED,
                    "authenticated_marker_visible",
                ),
            )
        },
        navigator=navigator,
        secret=secret,
        audit=audit,
    )

    result = await service.run(_command(), initial_capture=_capture("capture-initial"))

    assert result.status is H5AuthStatus.ALREADY_AUTHENTICATED
    assert result.final_capture.capture_id == "capture-root"
    assert result.attempted is False
    assert result.needs_review is False
    assert secret.calls == 0
    assert [call["operation"] for call in navigator.calls] == ["navigate_root"]


@pytest.mark.asyncio
async def test_active_login_traverses_profile_password_and_verifies_login() -> None:
    events: list[str] = []
    navigator = _Navigator(
        click_captures=[
            "capture-profile",
            "capture-login",
            "capture-password",
            "capture-verify-profile",
        ],
        observe_captures=["capture-after-submit"],
        events=events,
    )
    secret = _SecretRuntime(
        H5SecretInputResult(
            submitted=True,
            fields_scrubbed=True,
            hostname="h5.example.com",
            reason_code="secret_form_submitted",
        ),
        events=events,
    )
    audit = _Audit(events=events)
    service = _service(
        pages={
            "capture-initial": _page(
                "capture-initial",
                _observation(
                    H5AuthPageKind.UNKNOWN,
                    "profile_entry_available",
                    profile_target="我的",
                ),
            ),
            "capture-profile": _page(
                "capture-profile",
                _observation(
                    H5AuthPageKind.PROFILE_GUEST,
                    "profile_login_entry_visible",
                    login_target="立即登录",
                ),
            ),
            "capture-login": _page(
                "capture-login",
                _observation(
                    H5AuthPageKind.LOGIN_SWITCHABLE,
                    "password_mode_available",
                    password_mode_target="密码登录",
                ),
            ),
            "capture-password": _page(
                "capture-password",
                _observation(
                    H5AuthPageKind.LOGIN_PASSWORD,
                    "password_form_ready",
                    phone_target="请输入手机号",
                    password_target="请输入密码",
                    submit_target="登录",
                ),
            ),
            "capture-after-submit": _page(
                "capture-after-submit",
                _observation(
                    H5AuthPageKind.UNKNOWN,
                    "profile_entry_available",
                    profile_target="我的",
                ),
            ),
            "capture-verify-profile": _page(
                "capture-verify-profile",
                _observation(
                    H5AuthPageKind.PROFILE_AUTHENTICATED,
                    "authenticated_marker_visible",
                ),
            ),
        },
        navigator=navigator,
        secret=secret,
        audit=audit,
    )

    result = await service.run(_command(), initial_capture=_capture("capture-initial"))

    assert result.status is H5AuthStatus.AUTHENTICATED
    assert result.credential_scope == "global"
    assert result.attempted is True
    assert result.needs_review is False
    assert result.final_capture.capture_id == "capture-root"
    assert secret.calls == 1
    assert events.index("audit:credential_submit_planned") < events.index("secret_submit")
    assert [call["target"] for call in navigator.calls] == [
        "我的",
        "立即登录",
        "密码登录",
        "",
        "我的",
        "root",
    ]
    rendered_audit = json.dumps(audit.records, ensure_ascii=False)
    assert "selector" not in rendered_audit
    assert "请输入密码" not in rendered_audit


@pytest.mark.asyncio
async def test_registration_success_notice_is_closed_before_profile_verification() -> None:
    navigator = _Navigator(
        click_captures=["capture-profile"],
        observe_captures=["capture-success"],
    )
    secret = _SecretRuntime(
        H5SecretInputResult(
            submitted=True,
            fields_scrubbed=True,
            hostname="h5.example.com",
            reason_code="secret_form_submitted",
        )
    )
    audit = _Audit()
    service = _service(
        pages={
            "capture-initial": _page(
                "capture-initial",
                _observation(
                    H5AuthPageKind.LOGIN_PASSWORD,
                    "credential_form_ready",
                    phone_target="请输入手机号",
                    password_target="请输入密码",
                    submit_target="注册",
                    registration_submit_target="注册",
                ),
            ),
            "capture-success": _page(
                "capture-success",
                _observation(
                    H5AuthPageKind.SUCCESS_NOTICE,
                    "registration_success_notice_visible",
                    success_dismiss_target="关闭当前弹窗",
                ),
            ),
            "capture-profile": _page(
                "capture-profile",
                _observation(
                    H5AuthPageKind.PROFILE_AUTHENTICATED,
                    "profile_identity_visible",
                ),
            ),
        },
        navigator=navigator,
        secret=secret,
        audit=audit,
        submission_mode="register_if_available",
    )

    result = await service.run(_command(), initial_capture=_capture("capture-initial"))

    assert result.status is H5AuthStatus.AUTHENTICATED
    assert [call["target"] for call in navigator.calls] == [
        "",
        "关闭当前弹窗",
        "root",
    ]


@pytest.mark.asyncio
async def test_explicit_registration_mode_chooses_registration_submit_target() -> None:
    navigator = _Navigator()
    secret = _SecretRuntime(
        H5SecretInputResult(
            submitted=False,
            fields_scrubbed=True,
            hostname="h5.example.com",
            reason_code="expected_test_stop",
        )
    )
    audit = _Audit()
    service = H5AuthBootstrapService(
        credential_resolver=_Credentials(submission_mode="register_if_available"),
        page_reader=_PageReader(
            {
                "capture-initial": _page(
                    "capture-initial",
                    _observation(
                        H5AuthPageKind.LOGIN_PASSWORD,
                        "dual_login_registration_form_ready",
                        phone_target="请输入手机号",
                        password_target="请输入密码",
                        submit_target="登录",
                        registration_submit_target="注册",
                    ),
                ),
            }
        ),
        navigator=navigator,
        secret_runtime=secret,
        audit=audit,
    )

    result = await service.run(_command(), initial_capture=_capture("capture-initial"))

    assert result.status is H5AuthStatus.FAILED
    assert secret.calls == 1
    assert secret.commands[0].submit_semantic_target == "注册"


@pytest.mark.asyncio
async def test_challenge_only_page_continues_as_guest_and_requires_review() -> None:
    navigator = _Navigator()
    secret = _SecretRuntime(
        H5SecretInputResult(
            submitted=False,
            fields_scrubbed=True,
            hostname="h5.example.com",
            reason_code="unused",
        )
    )
    audit = _Audit()
    service = _service(
        pages={
            "capture-initial": _page(
                "capture-initial",
                _observation(
                    H5AuthPageKind.LOGIN_CHALLENGE,
                    "unsupported_login_challenge",
                ),
            )
        },
        navigator=navigator,
        secret=secret,
        audit=audit,
    )

    result = await service.run(_command(), initial_capture=_capture("capture-initial"))

    assert result.status is H5AuthStatus.GUEST_ONLY
    assert result.reason_code == "unsupported_login_challenge"
    assert result.needs_review is True
    assert result.final_capture.capture_id == "capture-root"
    assert secret.calls == 0


@pytest.mark.asyncio
async def test_failed_password_submission_is_not_retried() -> None:
    navigator = _Navigator()
    secret = _SecretRuntime(
        H5SecretInputResult(
            submitted=False,
            fields_scrubbed=True,
            hostname="h5.example.com",
            reason_code="secret_form_submit_failed",
        )
    )
    audit = _Audit()
    service = _service(
        pages={
            "capture-initial": _page(
                "capture-initial",
                _observation(
                    H5AuthPageKind.LOGIN_PASSWORD,
                    "password_form_ready",
                    phone_target="手机号",
                    password_target="密码",
                    submit_target="登录",
                ),
            )
        },
        navigator=navigator,
        secret=secret,
        audit=audit,
    )

    result = await service.run(_command(), initial_capture=_capture("capture-initial"))

    assert result.status is H5AuthStatus.FAILED
    assert result.reason_code == "secret_form_submit_failed"
    assert result.attempted is True
    assert result.needs_review is True
    assert secret.calls == 1


@pytest.mark.asyncio
async def test_no_request_submission_observes_scrubbed_failure_state() -> None:
    navigator = _Navigator(observe_captures=["capture-submit-failure"])
    secret = _SecretRuntime(
        H5SecretInputResult(
            submitted=False,
            fields_scrubbed=True,
            hostname="h5.example.com",
            reason_code="secret_form_submit_no_request",
        )
    )
    audit = _Audit()
    service = _service(
        pages={
            "capture-initial": _page(
                "capture-initial",
                _observation(
                    H5AuthPageKind.LOGIN_PASSWORD,
                    "password_form_ready",
                    phone_target="手机号",
                    password_target="密码",
                    submit_target="登录",
                ),
            )
        },
        navigator=navigator,
        secret=secret,
        audit=audit,
    )

    result = await service.run(_command(), initial_capture=_capture("capture-initial"))

    assert result.status is H5AuthStatus.FAILED
    assert result.reason_code == "secret_form_submit_no_request"
    assert result.final_capture.capture_id == "capture-root"
    assert [call["operation"] for call in navigator.calls] == [
        "observe",
        "navigate_root",
    ]
    assert navigator.calls[0]["source"] == "capture-initial"
    assert navigator.calls[1]["source"] == "capture-submit-failure"


@pytest.mark.asyncio
async def test_recovered_attempted_run_observes_without_resubmitting() -> None:
    navigator = _Navigator()
    secret = _SecretRuntime(
        H5SecretInputResult(
            submitted=True,
            fields_scrubbed=True,
            hostname="h5.example.com",
            reason_code="unused",
        )
    )
    audit = _Audit(attempted=True)
    service = _service(
        pages={
            "capture-initial": _page(
                "capture-initial",
                _observation(
                    H5AuthPageKind.LOGIN_PASSWORD,
                    "password_form_ready",
                    phone_target="手机号",
                    password_target="密码",
                    submit_target="登录",
                ),
            )
        },
        navigator=navigator,
        secret=secret,
        audit=audit,
    )

    result = await service.run(_command(), initial_capture=_capture("capture-initial"))

    assert result.status is H5AuthStatus.FAILED
    assert result.reason_code == "credential_submit_already_recorded"
    assert result.attempted is True
    assert secret.calls == 0


@pytest.mark.asyncio
async def test_unscrubbed_secret_fields_abort_before_any_capture() -> None:
    navigator = _Navigator()
    secret = _SecretRuntime(
        H5SecretInputResult(
            submitted=False,
            fields_scrubbed=False,
            hostname="h5.example.com",
            reason_code="secret_fields_scrub_failed",
        )
    )
    service = _service(
        pages={
            "capture-initial": _page(
                "capture-initial",
                _observation(
                    H5AuthPageKind.LOGIN_PASSWORD,
                    "password_form_ready",
                    phone_target="手机号",
                    password_target="密码",
                    submit_target="登录",
                ),
            )
        },
        navigator=navigator,
        secret=secret,
        audit=_Audit(),
    )

    with pytest.raises(H5AuthUnsafeSessionError, match="无法确认已清空"):
        await service.run(_command(), initial_capture=_capture("capture-initial"))

    assert [call["operation"] for call in navigator.calls] == []


@pytest.mark.asyncio
async def test_cross_hostname_page_fails_before_click_or_secret() -> None:
    navigator = _Navigator()
    secret = _SecretRuntime(
        H5SecretInputResult(
            submitted=True,
            fields_scrubbed=True,
            hostname="h5.example.com",
            reason_code="unused",
        )
    )
    service = _service(
        pages={
            "capture-initial": _page(
                "capture-initial",
                _observation(
                    H5AuthPageKind.PROFILE_GUEST,
                    "profile_login_entry_visible",
                    login_target="登录",
                ),
                hostname="identity.example.net",
            )
        },
        navigator=navigator,
        secret=secret,
        audit=_Audit(),
    )

    result = await service.run(_command(), initial_capture=_capture("capture-initial"))

    assert result.status is H5AuthStatus.FAILED
    assert result.reason_code == "auth_page_cross_hostname"
    assert secret.calls == 0
    assert [call["operation"] for call in navigator.calls] == ["navigate_root"]


class _PhaseRepository:
    def __init__(
        self,
        *,
        existing_run: SurfacePhaseRunSnapshot | None = None,
        existing_steps: tuple[SurfacePhaseStepSnapshot, ...] = (),
    ) -> None:
        self.run = existing_run
        self.steps = list(existing_steps)
        self.start_calls: list[dict[str, object]] = []
        self.record_calls: list[dict[str, object]] = []
        self.finish_calls: list[dict[str, object]] = []

    async def get_run(self, *, phase_run_id: str):
        if self.run is None or self.run.phase_run_id != phase_run_id:
            return None
        return self.run

    async def list_steps(self, *, phase_run_id: str):
        assert self.run is not None
        assert phase_run_id == self.run.phase_run_id
        return tuple(self.steps)

    async def start_run(self, **kwargs):
        self.start_calls.append(kwargs)
        self.run = SurfacePhaseRunSnapshot(
            phase_run_id="surface_phase_run:test-auth",
            task_id=kwargs["task_id"],
            session_id=kwargs["session_id"],
            surface_kind=kwargs["surface_kind"],
            phase_key=kwargs["phase_key"],
            phase_protocol_version=kwargs["phase_protocol_version"],
            idempotency_key=kwargs["idempotency_key"],
            source_capture_id=kwargs["source_capture_id"],
            status=SurfacePhaseRunStatus.RUNNING,
            command_sequence=kwargs["permit"].command_sequence,
            finalization_epoch=kwargs["permit"].finalization_epoch,
            row_version=0,
            context_payload=kwargs["context_payload"],
        )
        return self.run

    async def record_step(self, **kwargs):
        self.record_calls.append(kwargs)
        assert self.run is not None
        step_index = len(self.steps) + 1
        step = SurfacePhaseStepSnapshot(
            phase_step_id=f"phase-step-{step_index}",
            phase_run_id=kwargs["phase_run_id"],
            task_id=self.run.task_id,
            session_id=self.run.session_id,
            step_key=kwargs["step_key"],
            step_index=step_index,
            status=kwargs["status"],
            decision_status=kwargs["decision_status"],
            reason_code=kwargs["reason_code"],
            source_capture_id=kwargs["source_capture_id"],
            decision_payload=kwargs["decision_payload"],
            action_execution_id=kwargs.get("action_execution_id"),
            result_capture_id=kwargs.get("result_capture_id"),
        )
        self.steps.append(step)
        self.run = replace(
            self.run,
            row_version=self.run.row_version + 1,
            step_count=step_index,
        )
        return step

    async def finish_run(self, **kwargs):
        self.finish_calls.append(kwargs)
        assert self.run is not None
        self.run = replace(
            self.run,
            status=kwargs["status"],
            terminal_reason_code=kwargs["terminal_reason_code"],
            finished_at=datetime.now(UTC),
            row_version=self.run.row_version + 1,
        )
        return self.run


@pytest.mark.asyncio
async def test_surface_phase_audit_persists_only_safe_auth_facts() -> None:
    repository = _PhaseRepository()
    audit = SurfacePhaseH5AuthAudit(
        repository=repository,
        session_command_gate=InProcessSessionCommandGate(),
    )
    command = _command()
    state = await audit.begin(
        command=command,
        source_capture=_capture("capture-initial"),
        credential_scope="global",
    )

    state = await audit.record(
        state,
        step_key="credential_submit_planned",
        status=H5AuthStatus.CHECKING,
        reason_code="credential_submit_planned",
        source_capture=_capture("capture-initial"),
    )
    await audit.finish(
        state,
        status=H5AuthStatus.FAILED,
        reason_code="secret_form_submit_failed",
    )

    assert state.attempted is True
    assert repository.start_calls[0]["phase_key"] == "h5_auth_bootstrap"
    assert repository.start_calls[0]["phase_protocol_version"] == "h5-auth-bootstrap.v1"
    persisted = json.dumps(
        {
            "context": repository.start_calls[0]["context_payload"],
            "step": repository.record_calls[0]["decision_payload"],
        },
        ensure_ascii=False,
    )
    assert json.loads(persisted) == {
        "context": {
            "credential_scope": "global",
            "target_hostname": "h5.example.com",
        },
        "step": {
            "credential_scope": "global",
            "reason_code": "credential_submit_planned",
            "status": "checking",
        },
    }
    assert "selector" not in persisted
    assert "手机号" not in persisted
    assert "密码" not in persisted
    assert repository.finish_calls[0]["status"] is SurfacePhaseRunStatus.FAILED


@pytest.mark.asyncio
async def test_terminal_surface_phase_recovery_short_circuits_state_machine() -> None:
    command = _command()
    run = SurfacePhaseRunSnapshot(
        phase_run_id=build_surface_phase_run_id(
            task_id=command.task_id,
            session_id=command.session_id,
            phase_key="h5_auth_bootstrap",
            idempotency_key=f"h5-auth:{command.agent_run_id}",
        ),
        task_id=command.task_id,
        session_id=command.session_id,
        surface_kind=SurfaceKind.WEB,
        phase_key="h5_auth_bootstrap",
        phase_protocol_version="h5-auth-bootstrap.v1",
        idempotency_key=f"h5-auth:{command.agent_run_id}",
        source_capture_id="capture-original",
        status=SurfacePhaseRunStatus.COMPLETED,
        command_sequence=1,
        finalization_epoch=0,
        row_version=3,
        context_payload={
            "credential_scope": "global",
            "target_hostname": "h5.example.com",
        },
        step_count=2,
        terminal_reason_code="password_login_verified",
        finished_at=datetime.now(UTC),
    )
    steps = (
        SurfacePhaseStepSnapshot(
            phase_step_id="phase-step-planned",
            phase_run_id=run.phase_run_id,
            task_id=run.task_id,
            session_id=run.session_id,
            step_key="credential_submit_planned",
            step_index=1,
            status=SurfacePhaseStepStatus.ACTION_PLANNED,
            decision_status="checking",
            reason_code="credential_submit_planned",
            source_capture_id="capture-original",
            decision_payload={"status": "checking"},
        ),
        SurfacePhaseStepSnapshot(
            phase_step_id="phase-step-terminal",
            phase_run_id=run.phase_run_id,
            task_id=run.task_id,
            session_id=run.session_id,
            step_key="return_to_task_root",
            step_index=2,
            status=SurfacePhaseStepStatus.COMPLETED,
            decision_status="authenticated",
            reason_code="password_login_verified",
            source_capture_id="capture-verify",
            result_capture_id="capture-root-old",
            decision_payload={
                "status": "authenticated",
                "reason_code": "password_login_verified",
            },
        ),
    )
    audit = SurfacePhaseH5AuthAudit(
        repository=_PhaseRepository(existing_run=run, existing_steps=steps),
        session_command_gate=InProcessSessionCommandGate(),
    )
    navigator = _Navigator()
    secret = _SecretRuntime(
        H5SecretInputResult(
            submitted=True,
            fields_scrubbed=True,
            hostname="h5.example.com",
            reason_code="unused",
        )
    )
    service = H5AuthBootstrapService(
        credential_resolver=_Credentials(),
        page_reader=_PageReader(
            {
                "capture-recovered-root": _page(
                    "capture-recovered-root",
                    _observation(
                        H5AuthPageKind.PROFILE_AUTHENTICATED,
                        "authenticated_marker_visible",
                    ),
                )
            }
        ),
        navigator=navigator,
        secret_runtime=secret,
        audit=audit,
    )

    result = await service.run(command, initial_capture=_capture("capture-recovered-root"))

    assert result.status is H5AuthStatus.AUTHENTICATED
    assert result.reason_code == "password_login_verified"
    assert result.attempted is True
    assert result.final_capture.capture_id == "capture-recovered-root"
    assert result.needs_review is False
    assert secret.calls == 0
    assert navigator.calls == []


@pytest.mark.asyncio
async def test_terminal_authenticated_audit_does_not_trust_unverified_new_session() -> None:
    command = _command()
    run = SurfacePhaseRunSnapshot(
        phase_run_id=build_surface_phase_run_id(
            task_id=command.task_id,
            session_id=command.session_id,
            phase_key="h5_auth_bootstrap",
            idempotency_key=f"h5-auth:{command.agent_run_id}",
        ),
        task_id=command.task_id,
        session_id=command.session_id,
        surface_kind=SurfaceKind.WEB,
        phase_key="h5_auth_bootstrap",
        phase_protocol_version="h5-auth-bootstrap.v1",
        idempotency_key=f"h5-auth:{command.agent_run_id}",
        source_capture_id="capture-original",
        status=SurfacePhaseRunStatus.COMPLETED,
        command_sequence=1,
        finalization_epoch=0,
        row_version=3,
        context_payload={
            "credential_scope": "global",
            "target_hostname": "h5.example.com",
        },
        step_count=2,
        terminal_reason_code="password_login_verified",
        finished_at=datetime.now(UTC),
    )
    steps = (
        SurfacePhaseStepSnapshot(
            phase_step_id="phase-step-planned",
            phase_run_id=run.phase_run_id,
            task_id=run.task_id,
            session_id=run.session_id,
            step_key="credential_submit_planned",
            step_index=1,
            status=SurfacePhaseStepStatus.ACTION_PLANNED,
            decision_status="checking",
            reason_code="credential_submit_planned",
            source_capture_id="capture-original",
            decision_payload={"status": "checking"},
        ),
        SurfacePhaseStepSnapshot(
            phase_step_id="phase-step-terminal",
            phase_run_id=run.phase_run_id,
            task_id=run.task_id,
            session_id=run.session_id,
            step_key="return_to_task_root",
            step_index=2,
            status=SurfacePhaseStepStatus.COMPLETED,
            decision_status="authenticated",
            reason_code="password_login_verified",
            source_capture_id="capture-verify",
            result_capture_id="capture-root-old",
            decision_payload={"status": "authenticated"},
        ),
    )
    secret = _SecretRuntime(
        H5SecretInputResult(
            submitted=True,
            fields_scrubbed=True,
            hostname="h5.example.com",
            reason_code="unused",
        )
    )
    service = H5AuthBootstrapService(
        credential_resolver=_Credentials(),
        page_reader=_PageReader(
            {
                "capture-new-session": _page(
                    "capture-new-session",
                    _observation(H5AuthPageKind.UNKNOWN, "auth_state_unknown"),
                )
            }
        ),
        navigator=_Navigator(),
        secret_runtime=secret,
        audit=SurfacePhaseH5AuthAudit(
            repository=_PhaseRepository(existing_run=run, existing_steps=steps),
            session_command_gate=InProcessSessionCommandGate(),
        ),
    )

    result = await service.run(
        command,
        initial_capture=_capture("capture-new-session"),
    )

    assert result.status is H5AuthStatus.FAILED
    assert result.reason_code == "terminal_auth_session_not_verified"
    assert result.attempted is True
    assert result.needs_review is True
    assert secret.calls == 0
