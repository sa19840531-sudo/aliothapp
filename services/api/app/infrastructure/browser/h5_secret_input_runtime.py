"""在受控 Web Session 内执行 H5 密码登录秘密输入。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    ExecuteInteractionRequest,
    SessionCommandGate,
)
from services.api.app.application.h5_secret_input import (
    H5SecretInputCommand,
    H5SecretInputResult,
)
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
)
from services.api.app.application.interaction_runtime import (
    SemanticTargetResolutionError,
    TrustedUiSnapshotReader,
)
from services.api.app.core.h5_auth_config import (
    FileH5CredentialConfigResolver,
)
from services.api.app.domain.interaction import InteractionOperation, SurfaceKind
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)


class H5SecretFormBrowser(Protocol):
    """专用浏览器秘密窗口；实现不得产生通用动作证据。"""

    async def submit_secret_form(self, **kwargs: Any) -> Any: ...


class PlaywrightH5SecretInputRuntime:
    """持 Session 命令租约解析目标并短暂解开凭证。"""

    def __init__(
        self,
        *,
        credential_resolver: FileH5CredentialConfigResolver,
        snapshot_reader: TrustedUiSnapshotReader,
        target_resolver: UiSnapshotSemanticTargetResolver,
        browser: H5SecretFormBrowser,
        session_command_gate: SessionCommandGate,
        command_validator: InteractionCommandValidator | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._credential_resolver = credential_resolver
        self._snapshot_reader = snapshot_reader
        self._target_resolver = target_resolver
        self._browser = browser
        self._session_command_gate = session_command_gate
        self._command_validator = command_validator
        self._now = now or (lambda: datetime.now(UTC))

    async def execute(self, command: H5SecretInputCommand) -> H5SecretInputResult:
        """离线兼容入口；正式组合根使用外层 Coordinator 持有同一个命令租约。"""
        if not isinstance(command, H5SecretInputCommand):
            raise TypeError("command 必须为 H5SecretInputCommand")
        async with self._session_command_gate.hold(
            task_id=command.task_id,
            session_id=command.session_id,
            surface_kind=SurfaceKind.WEB,
        ):
            return await self.execute_in_permit(command)

    async def execute_in_permit(
        self,
        command: H5SecretInputCommand,
        *,
        interaction_request: ExecuteInteractionRequest | None = None,
    ) -> H5SecretInputResult:
        """只在外层已经取得命令租约后调用，不重复获取租约。"""
        if not isinstance(command, H5SecretInputCommand):
            raise TypeError("command 必须为 H5SecretInputCommand")
        remaining_ms = int((command.deadline - self._now()).total_seconds() * 1000)
        if remaining_ms <= 0:
            return _failed(command, reason_code="secret_input_deadline_exceeded")
        timeout_ms = min(remaining_ms, 30_000)
        validation_options = {}
        if self._command_validator is not None:
            if interaction_request is None:
                return _failed(command, reason_code="secret_input_coordination_required")
            coordination = CaptureCoordinationCommand(
                agent_run_id=interaction_request.agent_run_id,
                intent=interaction_request.intent,
            )
            await self._command_validator.validate_for(
                coordination,
                expected_finalization_epoch=interaction_request.finalization_epoch,
            )

            async def validate_current_location(url: str) -> None:
                await self._command_validator.validate_runtime_location_for(
                    coordination,
                    url=url,
                    expected_finalization_epoch=interaction_request.finalization_epoch,
                )

            validation_options["pre_action_validation"] = validate_current_location
        try:
            snapshot = await self._snapshot_reader.read_ui_snapshot(
                task_id=command.task_id,
                page_capture_id=command.source_capture_id,
            )
            phone_selector = self._target_resolver.resolve_unique_selector(
                snapshot=snapshot,
                semantic_target=command.phone_semantic_target,
                operation=InteractionOperation.INPUT,
            )
            password_selector = self._target_resolver.resolve_unique_selector(
                snapshot=snapshot,
                semantic_target=command.password_semantic_target,
                operation=InteractionOperation.INPUT,
            )
            submit_selector = self._target_resolver.resolve_unique_selector(
                snapshot=snapshot,
                semantic_target=command.submit_semantic_target,
                operation=InteractionOperation.CLICK,
            )
        except SemanticTargetResolutionError as exc:
            return _failed(command, reason_code=exc.error_code.casefold())
        except Exception:  # noqa: BLE001 - 不把 Snapshot 异常链带出秘密窗口
            return _failed(command, reason_code="secret_target_resolution_failed")

        if self._command_validator is not None:
            selectors = {phone_selector, password_selector, submit_selector}
            elements = [
                item
                for item in snapshot.get("elements", [])
                if isinstance(item, dict) and item.get("selector") in selectors
            ]
            await self._command_validator.validate_resolved_target_for(
                coordination,
                trusted_target_texts=tuple(
                    str(item[key])
                    for item in elements
                    for key in ("text", "aria_label", "placeholder", "role")
                    if item.get(key)
                ),
                target_trust="dom_snapshot",
                target_role="form",
                expected_finalization_epoch=interaction_request.finalization_epoch,
            )

        try:
            secret = self._credential_resolver.read_secret(command.credential_ref)
            phone, password = secret.reveal_for_runtime()
        except Exception:  # noqa: BLE001 - 配置异常不得携带原始输入
            return _failed(command, reason_code="credential_unavailable")
        try:
            browser_result = await self._browser.submit_secret_form(
                session_id=command.session_id,
                target_hostname=command.target_hostname,
                phone_selector=phone_selector,
                password_selector=password_selector,
                submit_selector=submit_selector,
                submit_semantic_target=command.submit_semantic_target,
                allowed_request_hostnames=command.credential_ref.allowed_request_hostnames,
                phone=phone,
                password=password,
                timeout_ms=timeout_ms,
                **validation_options,
            )
        except Exception:  # noqa: BLE001 - 浏览器异常可能包含填入值，必须截断异常链
            return _failed(
                command,
                reason_code="browser_secret_submit_failed",
                fields_scrubbed=False,
            )
        finally:
            del phone, password, secret
        return H5SecretInputResult(
            submitted=bool(browser_result.submitted),
            fields_scrubbed=bool(browser_result.fields_scrubbed),
            hostname=str(browser_result.hostname),
            reason_code=str(browser_result.reason_code),
        )


def _failed(
    command: H5SecretInputCommand,
    *,
    reason_code: str,
    fields_scrubbed: bool = True,
) -> H5SecretInputResult:
    return H5SecretInputResult(
        submitted=False,
        fields_scrubbed=fields_scrubbed,
        hostname=command.target_hostname,
        reason_code=reason_code,
    )


__all__ = ["PlaywrightH5SecretInputRuntime"]
