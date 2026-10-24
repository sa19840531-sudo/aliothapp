"""Web V2 就绪首屏的只读全页补充截图。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal, Protocol

from services.api.app.application.capture_coordination import SessionCommandGate
from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.application.web_assessment_evidence import (
    validate_web_full_page_supplemental_png,
)
from services.api.app.application.web_full_page_stabilization import (
    WebFullPageStabilizationError,
    WebFullPageStabilizationPolicy,
)
from services.api.app.application.web_page_classification import WebReadyPageCapture
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebStageCursor
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureBrowser,
    PageCaptureMode,
    PageCaptureRequest,
)

WEB_FULL_PAGE_SUPPLEMENTAL_PROTOCOL_VERSION = "web-full-page-supplemental.v1"
# 与既有 Web 判定证据的单截图文件上限一致，补图不得绕开该存储边界。
WEB_FULL_PAGE_SUPPLEMENTAL_MAX_SCREENSHOT_BYTES = 16 * 1024 * 1024
logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class WebFullPageSupplementalCommand:
    task_id: str
    agent_run_id: str
    session_id: str
    source_capture_id: str
    source_url: str
    deadline: datetime
    source_eligibility: Literal["formal_ready", "trusted_pre_gate"] = "formal_ready"
    stage_cursor: WebStageCursor | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, kw_only=True)
class WebFullPageSupplementalResult:
    status: Literal["captured", "failed", "already_attempted"]
    source_capture_id: str
    supplemental_capture_id: str | None
    screenshot_artifact_id: str | None
    reason_code: str
    screenshot_artifact_ids: tuple[str, ...] = ()
    manifest_artifact_id: str | None = None

    def __post_init__(self) -> None:
        if self.screenshot_artifact_id and not self.screenshot_artifact_ids:
            object.__setattr__(
                self,
                "screenshot_artifact_ids",
                (self.screenshot_artifact_id,),
            )
        if self.screenshot_artifact_ids and (
            self.screenshot_artifact_id != self.screenshot_artifact_ids[0]
        ):
            raise ValueError("长页首段引用必须与分段引用首项一致")


class WebFullPageSupplementalClaimRepository(Protocol):
    """一次性阶段领取与终态关闭端口。"""

    async def claim(
        self,
        *,
        command: WebFullPageSupplementalCommand,
        permit: SessionCommandPermit,
    ) -> str | None: ...

    async def finish(
        self,
        *,
        phase_id: str,
        status: Literal["completed", "failed"],
        reason_code: str,
        artifact_ids: tuple[str, ...],
    ) -> None: ...

    async def read_terminal(
        self,
        *,
        command: WebFullPageSupplementalCommand,
    ) -> WebFullPageSupplementalResult | None: ...


class WebReadyCaptureReader(Protocol):
    """只读返回已就绪 Web 主 Capture 的端口。"""

    async def read(self, *, capture_id: str, page_role: str) -> WebReadyPageCapture: ...

    async def read_current(self, *, page_role: str) -> WebReadyPageCapture: ...


class WebFullPageSupplementalCaptureService:
    """同一 ready Capture 最多补采一次全页截图，绝不改写主 Capture 游标。"""

    def __init__(
        self,
        *,
        browser: PageCaptureBrowser,
        persistence: object,
        claims: WebFullPageSupplementalClaimRepository,
        session_command_gate: SessionCommandGate,
        stabilization_policy: WebFullPageStabilizationPolicy | None = None,
    ) -> None:
        self._browser = browser
        self._persistence = persistence
        self._claims = claims
        self._session_command_gate = session_command_gate
        self._stabilization_policy = stabilization_policy or WebFullPageStabilizationPolicy()

    async def capture(
        self, command: WebFullPageSupplementalCommand
    ) -> WebFullPageSupplementalResult:
        return await self._capture_once(command)

    async def _capture_once(
        self, command: WebFullPageSupplementalCommand
    ) -> WebFullPageSupplementalResult:
        if command.session_id != f"web:{command.agent_run_id}":
            return _failed(command, "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT")

        completed = await self.read_completed(command)
        if completed is not None:
            return completed

        async with self._session_command_gate.hold(
            task_id=command.task_id,
            session_id=command.session_id,
            surface_kind=SurfaceKind.WEB,
        ) as permit:
            phase_id = await self._claims.claim(command=command, permit=permit)
            if phase_id is None:
                terminal = await self._claims.read_terminal(command=command)
                if (
                    terminal is not None
                    and terminal.source_capture_id == command.source_capture_id
                    and terminal.status in {"captured", "failed"}
                ):
                    return terminal
                return WebFullPageSupplementalResult(
                    status="already_attempted",
                    source_capture_id=command.source_capture_id,
                    supplemental_capture_id=None,
                    screenshot_artifact_id=None,
                    reason_code="WEB_FULL_PAGE_SCREENSHOT_ALREADY_ATTEMPTED",
                )
            if not _permit_matches(command=command, permit=permit):
                return await self._finish_failed(
                    command=command,
                    phase_id=phase_id,
                    reason_code="WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT",
                )
            if command.deadline <= datetime.now(UTC):
                return await self._finish_failed(
                    command=command,
                    phase_id=phase_id,
                    reason_code="WEB_FULL_PAGE_SCREENSHOT_TIMEOUT",
                )
            cancellation_audit = {}
            try:
                result = await asyncio.wait_for(
                    _capture_with_cancellation_audit(
                        self._browser,
                        _request(command, self._stabilization_policy),
                        cancellation_audit,
                    ),
                    timeout=_remaining_seconds(command.deadline),
                )
            except asyncio.CancelledError as exc:
                cancelled_stabilization = getattr(
                    exc, "stabilization_result", None
                ) or cancellation_audit.get("stabilization_result")
                await _finish_claim_after_cancellation(
                    self._finish_claim(
                        command,
                        phase_id=phase_id,
                        status="failed",
                        reason_code="WEB_FULL_PAGE_SCREENSHOT_CANCELLED",
                        artifact_ids=(),
                        **(
                            {"stabilization_audit": cancelled_stabilization.audit}
                            if cancelled_stabilization is not None
                            else {}
                        ),
                    )
                )
                raise
            except TimeoutError:
                timeout_stabilization = cancellation_audit.get("stabilization_result")
                return await self._finish_failed(
                    command=command,
                    phase_id=phase_id,
                    reason_code="WEB_FULL_PAGE_SCREENSHOT_TIMEOUT",
                    stabilization_audit=(
                        timeout_stabilization.audit if timeout_stabilization is not None else None
                    ),
                )
            except WebFullPageStabilizationError as exc:
                return await self._finish_failed(
                    command=command,
                    phase_id=phase_id,
                    reason_code=exc.reason_code,
                    stabilization_audit=exc.result.audit,
                )
            except Exception as exc:  # noqa: BLE001 - 浏览器异常必须消费本次 claim。
                return await self._finish_failed(
                    command=command,
                    phase_id=phase_id,
                    reason_code=_browser_error_reason(exc),
                )

            stabilization_audit = getattr(result, "metrics", {}).get("web_full_page_stabilization")
            audit_kwargs = (
                {"stabilization_audit": stabilization_audit}
                if stabilization_audit is not None
                else {}
            )
            validation_failure = _full_page_result_failure_reason(
                result=result,
                command=command,
            )
            if validation_failure is not None:
                validation_audit = {
                    **(stabilization_audit or {}),
                    "result_validation_failures": list(
                        _full_page_result_validation_failures(
                            result=result,
                            command=command,
                        )
                    ),
                }
                return await self._finish_failed(
                    command=command,
                    phase_id=phase_id,
                    reason_code=validation_failure,
                    stabilization_audit=validation_audit,
                )
            try:
                persisted = await self._persistence.persist(
                    task_id=command.task_id,
                    agent_run_id=command.agent_run_id,
                    result=result,
                    parent_page_capture_id=command.source_capture_id,
                    capture_role="supplemental",
                    capture_purpose="web_full_page_supplemental",
                    inherit_parent_semantics=False,
                    surface_session_id=command.session_id,
                    session_command_sequence=permit.command_sequence,
                    finalization_epoch=permit.finalization_epoch,
                )
            except asyncio.CancelledError:
                await _finish_claim_after_cancellation(
                    self._finish_claim(
                        command,
                        phase_id=phase_id,
                        status="failed",
                        reason_code="WEB_FULL_PAGE_SCREENSHOT_CANCELLED",
                        artifact_ids=(),
                        **audit_kwargs,
                    )
                )
                raise
            except Exception:  # noqa: BLE001 - 存储失败也必须成为一次性终态。
                return await self._finish_failed(
                    command=command,
                    phase_id=phase_id,
                    reason_code="WEB_FULL_PAGE_SCREENSHOT_PERSIST_FAILED",
                    **audit_kwargs,
                )

            screenshot_artifact_ids = _persisted_screenshot_artifact_ids(persisted)
            if len(screenshot_artifact_ids) != 1:
                return await self._finish_failed(
                    command=command,
                    phase_id=phase_id,
                    reason_code="WEB_FULL_PAGE_SCREENSHOT_PERSIST_FAILED",
                    **audit_kwargs,
                )
            try:
                await self._finish_claim(
                    command,
                    phase_id=phase_id,
                    status="completed",
                    reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
                    artifact_ids=screenshot_artifact_ids,
                    **audit_kwargs,
                )
            except asyncio.CancelledError:
                await _finish_claim_after_cancellation(
                    self._finish_claim(
                        command,
                        phase_id=phase_id,
                        status="completed",
                        reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
                        artifact_ids=screenshot_artifact_ids,
                        **audit_kwargs,
                    )
                )
                raise
            return WebFullPageSupplementalResult(
                status="captured",
                source_capture_id=command.source_capture_id,
                supplemental_capture_id=str(persisted.page_capture_id),
                screenshot_artifact_id=screenshot_artifact_ids[0],
                reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
                screenshot_artifact_ids=screenshot_artifact_ids,
                manifest_artifact_id=None,
            )

    async def read_completed(
        self, command: WebFullPageSupplementalCommand
    ) -> WebFullPageSupplementalResult | None:
        """先恢复已有父子证据，避免为已完成长图重新领取 Session。"""
        if command.session_id != f"web:{command.agent_run_id}":
            return None
        terminal = await self._claims.read_terminal(command=command)
        if (
            terminal is not None
            and terminal.status == "captured"
            and terminal.source_capture_id == command.source_capture_id
            and terminal.supplemental_capture_id
            and terminal.screenshot_artifact_id
        ):
            return terminal
        return None

    async def _finish_failed(
        self,
        *,
        command: WebFullPageSupplementalCommand,
        phase_id: str,
        reason_code: str,
        stabilization_audit: dict | None = None,
    ) -> WebFullPageSupplementalResult:
        try:
            await self._finish_claim(
                command,
                phase_id=phase_id,
                status="failed",
                reason_code=reason_code,
                artifact_ids=(),
                **(
                    {"stabilization_audit": stabilization_audit}
                    if stabilization_audit is not None
                    else {}
                ),
            )
        except asyncio.CancelledError:
            await _finish_claim_after_cancellation(
                self._finish_claim(
                    command,
                    phase_id=phase_id,
                    status="failed",
                    reason_code="WEB_FULL_PAGE_SCREENSHOT_CANCELLED",
                    artifact_ids=(),
                )
            )
            raise
        return _failed(command, reason_code)

    async def _finish_claim(self, command, **kwargs):
        if command.stage_cursor is not None:
            kwargs["stage_cursor"] = command.stage_cursor
        return await self._claims.finish(**kwargs)


class WebFullPageSupplementingReadyCaptureReader:
    """在保持主 Capture 不变的前提下，为分类附加一次只读全页补图。"""

    def __init__(
        self,
        *,
        delegate: WebReadyCaptureReader,
        supplemental_capture_service: WebFullPageSupplementalCaptureService,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        deadline: datetime,
    ) -> None:
        self._delegate = delegate
        self._supplemental_capture_service = supplemental_capture_service
        self._task_id = task_id
        self._agent_run_id = agent_run_id
        self._session_id = session_id
        self._deadline = deadline

    @property
    def source(self):
        """保留 ready reader 既有的只读来源观察入口。"""

        return self._delegate.source

    async def read(self, *, capture_id: str, page_role: str) -> WebReadyPageCapture:
        ready = await self._delegate.read(capture_id=capture_id, page_role=page_role)
        return await self._with_supplemental(ready)

    async def read_current(self, *, page_role: str) -> WebReadyPageCapture:
        ready = await self._delegate.read_current(page_role=page_role)
        return await self._with_supplemental(ready)

    async def _with_supplemental(self, ready: WebReadyPageCapture) -> WebReadyPageCapture:
        if ready.url is None:
            return replace(
                ready,
                supplemental_evidence_status="failed",
                supplemental_evidence_reason_code="WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT",
            )
        try:
            result = await self._supplemental_capture_service.capture(
                WebFullPageSupplementalCommand(
                    task_id=self._task_id,
                    agent_run_id=self._agent_run_id,
                    session_id=self._session_id,
                    source_capture_id=ready.capture_id,
                    source_url=ready.url,
                    deadline=self._deadline,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 补图基础设施异常不能破坏主证据。
            reason_code = _supplemental_infrastructure_reason(exc)
            logger.exception(
                "Web 全页补图基础设施失败",
                extra={
                    "reason_code": reason_code,
                    "source_capture_id": ready.capture_id,
                    "agent_run_id": self._agent_run_id,
                },
            )
            return replace(
                ready,
                supplemental_evidence_status="failed",
                supplemental_evidence_reason_code=reason_code,
            )
        if (
            result.status == "captured"
            and result.source_capture_id == ready.capture_id
            and result.screenshot_artifact_ids
        ):
            return replace(
                ready,
                supplemental_screenshot_artifact_ids=result.screenshot_artifact_ids,
                supplemental_evidence_status="captured",
                supplemental_evidence_reason_code=None,
            )
        return replace(
            ready,
            supplemental_evidence_status="failed",
            supplemental_evidence_reason_code=result.reason_code,
        )


def _supplemental_infrastructure_reason(exc: Exception) -> str:
    """保留已定义的长图失败码，其余领取或会话故障归为 claim 拒绝。"""

    diagnostic = str(exc)
    known = (
        "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT",
        "WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED",
        "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT",
        "WEB_FULL_PAGE_SCREENSHOT_PERSIST_FAILED",
        "WEB_FULL_PAGE_SCREENSHOT_CLAIM_REJECTED",
        "WEB_FULL_PAGE_SCREENSHOT_CANCELLED",
    )
    return next(
        (reason_code for reason_code in known if reason_code in diagnostic),
        "WEB_FULL_PAGE_SCREENSHOT_CLAIM_REJECTED",
    )


async def _capture_with_cancellation_audit(browser, request, audit_sink):
    """在 wait_for 把取消转换成超时之前，将浏览器清理审计保留到调用级容器。"""
    try:
        return await browser.capture(request)
    except asyncio.CancelledError as exc:
        if (result := getattr(exc, "stabilization_result", None)) is not None:
            audit_sink["stabilization_result"] = result
        raise


def _request(
    command: WebFullPageSupplementalCommand, stabilization_policy=None
) -> PageCaptureRequest:
    """只请求单张整页补图，不采新主 DOM 或推进业务游标。"""
    return PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE,
        capture_dom=False,
        capture_visible_text=False,
        capture_screenshot=True,
        full_page=True,
        web_capture_stage="web_full_page_supplemental",
        web_full_page_stabilization_policy=stabilization_policy,
        session_id=command.session_id,
        timeout_ms=max(1, int(_remaining_seconds(command.deadline) * 1000)),
    )


def _remaining_seconds(deadline: datetime) -> float:
    return max(0.001, (deadline - datetime.now(UTC)).total_seconds())


async def _finish_claim_after_cancellation(finish_operation) -> None:
    """尽力把已领取阶段写成终态，但绝不遮蔽原始取消。"""

    task = asyncio.create_task(finish_operation)
    try:
        await asyncio.shield(task)
    except BaseException:  # noqa: BLE001 - 终态写入失败不能替代原始取消。
        pass


def _permit_matches(
    *, command: WebFullPageSupplementalCommand, permit: SessionCommandPermit
) -> bool:
    return (
        permit.task_id == command.task_id
        and permit.session_id == command.session_id
        and permit.surface_kind is SurfaceKind.WEB
        and permit.current_capture_id == command.source_capture_id
    )


def _full_page_result_failure_reason(
    *, result: object, command: WebFullPageSupplementalCommand
) -> str | None:
    artifacts = list(getattr(result, "artifacts", ()) or ())
    screenshots = [
        item for item in artifacts if getattr(item, "artifact_type", None) == "screenshot"
    ]
    manifests = [
        item for item in artifacts if getattr(item, "artifact_type", None) == "page_coverage"
    ]
    if (
        getattr(result, "final_url", None) != command.source_url
        or not _uses_direct_full_page_contract(screenshots, manifests)
    ):
        return "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"
    if _full_page_png_bytes(screenshots[0]) is None:
        return "WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED"
    return (
        "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"
        if _direct_full_page_identity_failures(result, command)
        else None
    )


def _full_page_result_validation_failures(
    *, result: object, command: WebFullPageSupplementalCommand
) -> tuple[str, ...]:
    """只记录失败检查名，避免把页面内容或身份原值写入诊断。"""

    failures: list[str] = []
    artifacts = list(getattr(result, "artifacts", ()) or ())
    screenshots = [
        item for item in artifacts if getattr(item, "artifact_type", None) == "screenshot"
    ]
    manifests = [
        item for item in artifacts if getattr(item, "artifact_type", None) == "page_coverage"
    ]
    if not screenshots:
        failures.append("screenshot_presence")
    if getattr(result, "final_url", None) != command.source_url:
        failures.append("final_url_identity")
    if not _uses_direct_full_page_contract(screenshots, manifests):
        failures.append("direct_full_page_contract")
        return tuple(failures)
    if _full_page_png_bytes(screenshots[0]) is None:
        failures.append("png_payload")
    failures.extend(_direct_full_page_identity_failures(result, command))
    return tuple(failures)


def _uses_direct_full_page_contract(
    screenshots: list[object], manifests: list[object]
) -> bool:
    if manifests or len(screenshots) != 1:
        return False
    content = getattr(screenshots[0], "content", None)
    return bool(
        isinstance(content, Mapping)
        and content.get("full_page") is True
        and content.get("purpose") == "web_direct_full_page"
    )


def _direct_full_page_identity_failures(
    result: object, command: WebFullPageSupplementalCommand
) -> tuple[str, ...]:
    # 只检查截图所属页面和文档代次；动态内容完整性留给质检。
    metrics = getattr(result, "metrics", {})
    readiness = metrics.get("web_readiness") if isinstance(metrics, Mapping) else None
    if not isinstance(readiness, Mapping):
        return ("readiness_presence",)
    failures: list[str] = []
    if not (
        readiness.get("identity_before") == command.source_url
        and readiness.get("identity_after") == command.source_url
    ):
        failures.append("source_url_identity")
    if readiness.get("artifacts_same_epoch") is not True:
        failures.append("document_epoch")
    return tuple(failures)


def _full_page_png_bytes(screenshot: object) -> bytes | None:
    """解码并有界校验浏览器返回的完整 PNG，拒绝伪造或超限长图。"""
    content = getattr(screenshot, "content", None)
    if not isinstance(content, Mapping) or content.get("encoding") != "base64":
        return None
    encoded = content.get("data")
    if not isinstance(encoded, str):
        return None
    maximum_encoded_bytes = 4 * ((WEB_FULL_PAGE_SUPPLEMENTAL_MAX_SCREENSHOT_BYTES + 2) // 3)
    if len(encoded) > maximum_encoded_bytes:
        return None
    try:
        payload = base64.b64decode(encoded, validate=True)
        if len(payload) > WEB_FULL_PAGE_SUPPLEMENTAL_MAX_SCREENSHOT_BYTES:
            return None
        validate_web_full_page_supplemental_png(payload)
    except (RuntimeError, ValueError, binascii.Error):
        return None
    return payload


def _persisted_screenshot_artifact_ids(persisted: object) -> tuple[str, ...]:
    artifacts = tuple(getattr(persisted, "artifacts", ()) or ())
    screenshots = [
        str(getattr(item, "artifact_id", "") or "")
        for item in artifacts
        if getattr(item, "artifact_type", None) == "screenshot"
    ]
    return tuple(item for item in screenshots if item)


def _browser_error_reason(error: Exception) -> str:
    if _is_playwright_timeout(error):
        return "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT"
    message = str(error).casefold()
    if any(marker in message for marker in ("maximum", "too large", "pixel", "dimension", "size")):
        return "WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED"
    return "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"


def _is_playwright_timeout(error: Exception) -> bool:
    """Playwright 自定义超时不是内置 TimeoutError，必须单独识别。"""
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    except ImportError:
        return False
    return isinstance(error, PlaywrightTimeoutError)


def _failed(
    command: WebFullPageSupplementalCommand, reason_code: str
) -> WebFullPageSupplementalResult:
    return WebFullPageSupplementalResult(
        status="failed",
        source_capture_id=command.source_capture_id,
        supplemental_capture_id=None,
        screenshot_artifact_id=None,
        reason_code=reason_code,
    )
