"""星语 Official App API 的 HTTP 边界与私有设备会话管理。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from services.api.app.core.config import WorkflowSettings
from services.api.app.domain.interaction import (
    ActionIntent,
    AppInstallRequest,
    AppOpenRequest,
    CaptureProfile,
    SurfaceSessionRef,
    SurfaceSessionSpec,
)
from services.api.app.domain.interaction.validation import require_text

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 502, 503, 504})
ACTION_OUTCOME_UNKNOWN_ERROR_CODE = "ACTION_OUTCOME_UNKNOWN"
_CAPTURE_MIN_RETRIES = 3
# 连续 502／408 时拉长退避，避免打爆同一坏通道（2s / 4s / 8s）
_CAPTURE_RETRY_BACKOFF_BASE_SECONDS = 2.0
_CAPTURE_RETRY_BACKOFF_MAX_SECONDS = 8.0
# 单帧最坏：预刷新 25s + 首次 pull 90s + 2×(refresh 25s + 短 pull 45s)≈255s。
# Live Capture 还要为前台身份稳定最多再采 3 次（device_bridge._CAPTURE_IDENTITY_ATTEMPTS），
# 若 floor 仍按单帧 270s，客户端会先 ReadTimeout，表面成「Official App 截图调用失败」。
# 预算：首帧刷新 25 + 3×(90 + 2×(25+45)) ≈ 715 → floor 780。
_CAPTURE_TIMEOUT_FLOOR_SECONDS = 780.0
_FORCE_ADB_REFRESH_HEADER = "X-Alioth-Force-Adb-Refresh"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_AUX_ARTIFACT_TYPES = frozenset(
    {"device_log", "network_trace", "fault_artifact", "clipboard"}
)
_MAX_AUX_RESPONSE_BYTES = 1024 * 1024
_OBSERVABILITY_FIELDS = frozenset(
    {
        "task_id",
        "session_id",
        "request_id",
        "capture_id",
        "artifact_id",
        "checksum",
        "size",
        "error_code",
    }
)


@dataclass(frozen=True, kw_only=True)
class OfficialSessionResponse:
    """建立 Official 会话后的私有设备句柄，仅允许停留在 Adapter 边界内。"""

    request_id: str
    session_handle: str

    def __post_init__(self) -> None:
        require_text(self.request_id, "request_id")
        require_text(self.session_handle, "session_handle")


@dataclass(frozen=True, kw_only=True)
class OfficialInstallResponse:
    """Official 安装调用的结构化结果。"""

    request_id: str
    succeeded: bool
    retryable: bool
    installed_package: str | None = None
    reused_existing: bool = False
    error_code: str | None = None


@dataclass(frozen=True, kw_only=True)
class OfficialOpenResponse:
    """Official 打开 App 调用的结构化结果。"""

    request_id: str
    succeeded: bool
    retryable: bool
    package_name: str
    foreground_verified: bool
    activity: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, kw_only=True)
class OfficialCaptureResponse:
    """截图端点的唯一输出；禁止在此 DTO 中添加 DOM、UI Tree 或 OCR 字段。"""

    content_type: str
    png_bytes: bytes
    width: int
    height: int
    orientation: str
    package_name: str | None
    activity: str | None
    declared_sha256: str | None


@dataclass(frozen=True, kw_only=True)
class OfficialUiTreeResponse:
    """独立 UI Tree 端点输出；与截图 DTO 分离，避免污染 PNG 契约。"""

    content_type: str
    xml_text: str
    request_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class OfficialAuxCaptureResponse:
    """device_log / network_trace / fault_artifact 的 JSON 制品。"""

    artifact_type: str
    content_type: str
    payload: Mapping[str, Any]
    request_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class OfficialActionResponse:
    """Official 动作调用的结构化结果与可审计动作轨迹。"""

    request_id: str
    action_execution_id: str
    succeeded: bool
    retryable: bool
    action_trace: tuple[Mapping[str, Any], ...] = ()
    error_code: str | None = None


class OfficialAppApiClient(Protocol):
    """Official App API 的可替换边界，运行时不会直接持有 HTTP 实现。"""

    async def start_session(self, spec: SurfaceSessionSpec) -> OfficialSessionResponse: ...

    async def reattach_session(self, spec: SurfaceSessionSpec) -> OfficialSessionResponse: ...

    async def install(self, request: AppInstallRequest) -> OfficialInstallResponse: ...

    async def open(self, request: AppOpenRequest) -> OfficialOpenResponse: ...

    async def capture_png(
        self,
        session: SurfaceSessionRef,
        profile: CaptureProfile,
    ) -> OfficialCaptureResponse: ...

    async def capture_ui_tree(
        self,
        session: SurfaceSessionRef,
        profile: CaptureProfile,
    ) -> OfficialUiTreeResponse: ...

    async def capture_aux(
        self,
        session: SurfaceSessionRef,
        *,
        artifact_type: str,
        package_name: str | None = None,
    ) -> OfficialAuxCaptureResponse: ...

    async def execute(
        self,
        session: SurfaceSessionRef,
        intent: ActionIntent,
        action_execution_id: str,
        execution_context: Mapping[str, Any] | None = None,
    ) -> OfficialActionResponse: ...

    async def close(self, session: SurfaceSessionRef, reason: str) -> None: ...


class HttpOfficialAppApiClient:
    """Official HTTP 客户端；外部句柄仅缓存在该私有对象中。"""

    def __init__(
        self,
        *,
        settings: WorkflowSettings,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 1,
    ) -> None:
        settings.validate_official_app_configuration()
        max_retries = _validate_max_retries(max_retries)
        self._settings = settings
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=settings.official_app_base_url,
            timeout=settings.official_app_timeout_seconds,
            headers={
                settings.official_app_auth_header_name: settings.official_app_auth_token,
            },
            transport=transport,
        )
        self._session_handles: dict[str, str] = {}
        self._capture_request_epoch = uuid.uuid4().hex[:16]
        self._capture_request_sequence = 0
        # 同 session 连续 Capture 暂态失败（502／408／504）计数；成功清零
        self._capture_transient_streak: dict[str, int] = {}

    async def aclose(self) -> None:
        """关闭由本客户端拥有的 HTTP 连接池。"""
        await self._client.aclose()

    async def start_session(self, spec: SurfaceSessionSpec) -> OfficialSessionResponse:
        response = await self._post_json(
            self._settings.official_app_start_session_path,
            payload={
                "task_id": spec.task_id,
                "session_id": spec.session_id,
                "surface_kind": spec.surface_kind.value,
                "account_environment_ref": spec.account_environment_ref,
                "platform_build_ref": spec.platform_build_ref,
                "device_ref": spec.device_ref,
                "options": dict(spec.options),
            },
            idempotency_key=f"session:{spec.session_id}",
        )
        result = _parse_session_response(response)
        self._session_handles[spec.session_id] = result.session_handle
        _log_success(
            task_id=spec.task_id,
            session_id=spec.session_id,
            request_id=result.request_id,
        )
        return result

    async def reattach_session(self, spec: SurfaceSessionSpec) -> OfficialSessionResponse:
        response = await self._post_json(
            self._settings.official_app_reattach_path,
            payload={
                "task_id": spec.task_id,
                "session_id": spec.session_id,
                "surface_kind": spec.surface_kind.value,
                "account_environment_ref": spec.account_environment_ref,
                "platform_build_ref": spec.platform_build_ref,
                "device_ref": spec.device_ref,
                "options": _reattach_options(spec.options),
            },
            idempotency_key=f"reattach:{spec.session_id}",
        )
        result = _parse_session_response(response)
        self._session_handles[spec.session_id] = result.session_handle
        _log_success(
            task_id=spec.task_id,
            session_id=spec.session_id,
            request_id=result.request_id,
        )
        return result

    async def install(self, request: AppInstallRequest) -> OfficialInstallResponse:
        # 大 APK 远端 push 常超过通用 timeout；Install 禁止盲目整段重试。
        install_timeout = float(
            getattr(self._settings, "official_app_install_timeout_seconds", 0) or 0
        )
        if install_timeout <= 0:
            install_timeout = max(
                float(self._settings.official_app_timeout_seconds),
                600.0,
            )
        response = await self._request_with_retry(
            method="POST",
            path=self._settings.official_app_install_path,
            payload={
                **request.to_dict(),
                "session_handle": self._session_handle(request.session_id),
            },
            idempotency_key=f"install:{request.idempotency_key}",
            max_retries=0,
            timeout=install_timeout,
        )
        result = _parse_install_response(response)
        _log_success(
            task_id=request.task_id,
            session_id=request.session_id,
            request_id=result.request_id,
        )
        return result

    async def open(self, request: AppOpenRequest) -> OfficialOpenResponse:
        response = await self._post_json(
            self._settings.official_app_open_path,
            payload={
                "task_id": request.task_id,
                "session_id": request.session_id,
                "package_name": request.package_name,
                "cold_start": request.cold_start,
                "activity": request.activity,
                "protocol_version": request.protocol_version,
                "session_handle": self._session_handle(request.session_id),
            },
            idempotency_key=f"open:{request.idempotency_key}",
        )
        result = _parse_open_response(response)
        _log_success(
            task_id=request.task_id,
            session_id=request.session_id,
            request_id=result.request_id,
        )
        return result

    async def capture_png(
        self,
        session: SurfaceSessionRef,
        profile: CaptureProfile,
    ) -> OfficialCaptureResponse:
        capture_retry_limit = max(self._max_retries, _CAPTURE_MIN_RETRIES)
        capture_timeout = _capture_request_timeout(self._settings)
        last_error: OfficialAppApiError | None = None
        for png_attempt in range(2):
            response = await self._request_with_retry(
                method="POST",
                path=self._settings.official_app_capture_path,
                payload={
                    "task_id": session.task_id,
                    "session_id": session.session_id,
                    "surface_ref": session.primary_surface_ref,
                    "capture_profile": profile.to_dict(),
                    "session_handle": self._session_handle(session.session_id),
                },
                idempotency_key=self._next_capture_idempotency_key(
                    kind="capture",
                    session_id=session.session_id,
                    discriminator=profile.profile_id,
                ),
                stream=True,
                max_retries=capture_retry_limit,
                retry_backoff=True,
                timeout=capture_timeout,
                capture_session_id=session.session_id,
            )
            try:
                try:
                    content = await _read_limited_bytes(
                        response,
                        max_bytes=self._settings.official_app_max_screenshot_bytes,
                    )
                except httpx.TimeoutException as error:
                    last_error = _transport_error(error, timeout=True)
                    self._note_capture_transient_failure(session.session_id)
                    if png_attempt < 1:
                        await _maybe_backoff_before_retry(
                            attempt=png_attempt,
                            enabled=True,
                        )
                        continue
                    raise last_error from error
                except httpx.RequestError as error:
                    last_error = _transport_error(error, timeout=False)
                    self._note_capture_transient_failure(session.session_id)
                    if png_attempt < 1:
                        await _maybe_backoff_before_retry(
                            attempt=png_attempt,
                            enabled=True,
                        )
                        continue
                    raise last_error from error
                if not _is_valid_png_bytes(content):
                    last_error = OfficialAppApiError(
                        "Official 截图不是有效 PNG",
                        error_code="CAPTURE_CONTRACT_INVALID",
                        retryable=True,
                    )
                    if png_attempt < 1:
                        self._note_capture_transient_failure(session.session_id)
                        await _maybe_backoff_before_retry(
                            attempt=png_attempt,
                            enabled=True,
                        )
                        continue
                    raise last_error
                try:
                    result = OfficialCaptureResponse(
                        content_type=response.headers.get("content-type", ""),
                        png_bytes=content,
                        width=_required_header_int(response, "x-alioth-width"),
                        height=_required_header_int(response, "x-alioth-height"),
                        orientation=_required_orientation_header(response),
                        package_name=_optional_header_text(
                            response, "x-alioth-package-name"
                        ),
                        activity=_optional_header_text(response, "x-alioth-activity"),
                        declared_sha256=_optional_header_text(
                            response, "x-alioth-sha256"
                        ),
                    )
                except OfficialAppApiError as error:
                    raise _response_contract_error(error) from error
                self._clear_capture_transient_streak(session.session_id)
                _log_success(
                    task_id=session.task_id,
                    session_id=session.session_id,
                    request_id=_optional_header_text(response, "x-request-id"),
                    checksum=hashlib.sha256(content).hexdigest(),
                    size=len(content),
                )
                return result
            finally:
                await response.aclose()
        if last_error is not None:
            raise last_error
        raise AssertionError("Official 截图重试循环意外结束")

    async def capture_ui_tree(
        self,
        session: SurfaceSessionRef,
        profile: CaptureProfile,
    ) -> OfficialUiTreeResponse:
        response = await self._request_with_retry(
            method="POST",
            path=self._settings.official_app_capture_ui_tree_path,
            payload={
                "task_id": session.task_id,
                "session_id": session.session_id,
                "surface_ref": session.primary_surface_ref,
                "capture_profile": profile.to_dict(),
                "session_handle": self._session_handle(session.session_id),
            },
            idempotency_key=self._next_capture_idempotency_key(
                kind="capture-ui-tree",
                session_id=session.session_id,
                discriminator=profile.profile_id,
            ),
            stream=True,
            max_retries=max(self._max_retries, _CAPTURE_MIN_RETRIES),
            retry_backoff=True,
            timeout=_capture_request_timeout(self._settings),
            capture_session_id=session.session_id,
        )
        try:
            content = await _read_limited_bytes(
                response,
                max_bytes=self._settings.official_app_max_screenshot_bytes,
            )
            text = content.decode("utf-8", errors="replace").strip()
            if "<hierarchy" not in text and not text.startswith("<?xml"):
                raise OfficialAppApiError(
                    "Official UI Tree 内容无效",
                    error_code="CAPTURE_CONTRACT_INVALID",
                    retryable=False,
                )
            result = OfficialUiTreeResponse(
                content_type=response.headers.get("content-type", "application/xml"),
                xml_text=text,
                request_id=_optional_header_text(response, "x-request-id"),
            )
            _log_success(
                task_id=session.task_id,
                session_id=session.session_id,
                request_id=result.request_id,
                size=len(content),
            )
            return result
        finally:
            await response.aclose()

    async def capture_aux(
        self,
        session: SurfaceSessionRef,
        *,
        artifact_type: str,
        package_name: str | None = None,
    ) -> OfficialAuxCaptureResponse:
        require_text(artifact_type, "artifact_type")
        if artifact_type not in _AUX_ARTIFACT_TYPES:
            raise OfficialAppApiError(
                "Official capture-aux artifact_type 不受支持",
                error_code="CAPTURE_CONTRACT_INVALID",
                retryable=False,
            )
        response = await self._post_json(
            self._settings.official_app_capture_aux_path,
            payload={
                "task_id": session.task_id,
                "session_id": session.session_id,
                "surface_ref": session.primary_surface_ref,
                "session_handle": self._session_handle(session.session_id),
                "artifact_type": artifact_type,
                "package_name": package_name,
            },
            idempotency_key=self._next_capture_idempotency_key(
                kind="capture-aux",
                session_id=session.session_id,
                discriminator=f"{artifact_type}:{package_name or '-'}",
            ),
        )
        if len(response.content) > _MAX_AUX_RESPONSE_BYTES:
            raise OfficialAppApiError(
                "Official capture-aux 响应超过大小上限",
                error_code="CAPTURE_CONTRACT_INVALID",
                retryable=False,
            )
        try:
            body = _json_mapping(response)
            response_artifact_type = _required_text(body, "artifact_type")
            if response_artifact_type != artifact_type:
                raise OfficialAppApiError("Official capture-aux 制品类型与请求不一致")
            content_type = _required_text(body, "content_type")
            if content_type.split(";", 1)[0].strip().casefold() != "application/json":
                raise OfficialAppApiError("Official capture-aux MIME 必须是 application/json")
            payload = body.get("payload")
            if not isinstance(payload, Mapping):
                raise OfficialAppApiError("Official capture-aux payload 无效")
            _validate_aux_payload_identity(
                payload,
                task_id=session.task_id,
                session_id=session.session_id,
                package_name=package_name,
            )
            result = OfficialAuxCaptureResponse(
                artifact_type=response_artifact_type,
                content_type=content_type,
                payload=dict(payload),
                request_id=_optional_text(body, "request_id"),
            )
        except OfficialAppApiError as error:
            raise OfficialAppApiError(
                "Official capture-aux 成功响应不符合合同",
                error_code="CAPTURE_CONTRACT_INVALID",
                retryable=False,
            ) from error
        _log_success(
            task_id=session.task_id,
            session_id=session.session_id,
            request_id=result.request_id,
        )
        return result

    def _next_capture_idempotency_key(
        self,
        *,
        kind: str,
        session_id: str,
        discriminator: str,
    ) -> str:
        """每次采集生成新键；单次 HTTP 重试仍复用该键，避免跨帧命中旧缓存。"""

        self._capture_request_sequence += 1
        scope_hash = hashlib.sha256(
            f"{session_id}:{discriminator}".encode()
        ).hexdigest()[:16]
        return (
            f"{kind}:{self._capture_request_epoch}:"
            f"{self._capture_request_sequence}:{scope_hash}"
        )

    async def execute(
        self,
        session: SurfaceSessionRef,
        intent: ActionIntent,
        action_execution_id: str,
        execution_context: Mapping[str, Any] | None = None,
    ) -> OfficialActionResponse:
        context = dict(execution_context or {})
        # 动作请求可能已经送达设备；即使服务端随后返回 502 或连接超时，也不
        # 能沿用通用 HTTP 重试，否则同一动作会再次触发设备副作用。未知结果
        # 交给上层 Action ledger 固化，后续恢复只能读取事实，不能重放 Driver。
        try:
            response = await self._request_with_retry(
                method="POST",
                path=self._settings.official_app_execute_path,
                payload={
                    "session_handle": self._session_handle(session.session_id),
                    "surface_ref": session.primary_surface_ref,
                    "action_execution_id": action_execution_id,
                    "intent": intent.to_dict(),
                    **({"execution_context": context} if context else {}),
                },
                idempotency_key=f"action:{action_execution_id}",
                max_retries=0,
            )
        except OfficialAppApiError as error:
            if _is_action_outcome_unknown_error(error):
                raise _unknown_action_outcome_error(error) from error
            raise
        try:
            result = _parse_action_response(response)
        except OfficialAppApiError as error:
            # 成功 HTTP 响应的动作合同损坏时，设备是否已执行同样无法证明。
            raise _unknown_action_outcome_error(error) from error
        _log_success(
            session_id=session.session_id,
            request_id=result.request_id,
        )
        return result

    async def close(self, session: SurfaceSessionRef, reason: str) -> None:
        try:
            await self._post_json(
                self._settings.official_app_close_path,
                payload={
                    "task_id": session.task_id,
                    "session_id": session.session_id,
                    "surface_ref": session.primary_surface_ref,
                    "session_handle": self._session_handle(session.session_id),
                    "reason": reason,
                },
                idempotency_key=f"close:{session.session_id}:{reason}",
            )
        finally:
            self._session_handles.pop(session.session_id, None)

    async def _post_json(
        self,
        path: str,
        *,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> httpx.Response:
        response = await self._request_with_retry(
            method="POST",
            path=path,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        return response

    async def _request_with_retry(
        self,
        *,
        method: str,
        path: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        stream: bool = False,
        max_retries: int | None = None,
        retry_backoff: bool = False,
        timeout: httpx.Timeout | float | None = None,
        capture_session_id: str | None = None,
    ) -> httpx.Response:
        """使用幂等键发送可安全重放的 Official API 请求。"""
        retry_limit = (
            self._max_retries
            if max_retries is None
            else _validate_max_retries(max_retries)
        )
        for attempt in range(retry_limit + 1):
            body = dict(payload)
            headers: dict[str, str] = {"Idempotency-Key": idempotency_key}
            force_refresh = False
            if capture_session_id is not None:
                streak = self._capture_transient_streak.get(capture_session_id, 0)
                # 第 2 次起（含本请求内 attempt≥1 或跨请求 streak≥1）强制换通道
                force_refresh = streak >= 1 or attempt >= 1
                if force_refresh:
                    # 同键重送必须保持请求体不变；刷新提示仅在服务端缓存未命中时使用。
                    headers[_FORCE_ADB_REFRESH_HEADER] = "1"
            # httpx 0.28+：单次请求超时挂在 build_request，send() 不再接受 timeout=
            build_kwargs: dict[str, Any] = {
                "json": body,
                "headers": headers,
            }
            if timeout is not None:
                build_kwargs["timeout"] = timeout
            request = self._client.build_request(method, path, **build_kwargs)
            try:
                response = await self._client.send(
                    request,
                    stream=stream,
                )
            except httpx.TimeoutException as error:
                if capture_session_id is not None:
                    self._note_capture_transient_failure(capture_session_id)
                if attempt < retry_limit:
                    if force_refresh:
                        logger.warning(
                            "official_capture_transient_retry session_id=%s attempt=%s "
                            "streak=%s reason=timeout force_adb_refresh=1",
                            capture_session_id,
                            attempt,
                            self._capture_transient_streak.get(capture_session_id or "", 0),
                        )
                    await _maybe_backoff_before_retry(
                        attempt=attempt,
                        enabled=retry_backoff,
                    )
                    continue
                raise _transport_error(error, timeout=True) from error
            except httpx.RequestError as error:
                if capture_session_id is not None:
                    self._note_capture_transient_failure(capture_session_id)
                if attempt < retry_limit:
                    await _maybe_backoff_before_retry(
                        attempt=attempt,
                        enabled=retry_backoff,
                    )
                    continue
                raise _transport_error(error, timeout=False) from error

            if response.is_success:
                if capture_session_id is not None:
                    self._clear_capture_transient_streak(capture_session_id)
                return response

            error = _http_status_error(response)
            if error.retryable and attempt < retry_limit:
                if capture_session_id is not None:
                    self._note_capture_transient_failure(capture_session_id)
                    logger.warning(
                        "official_capture_transient_retry session_id=%s attempt=%s "
                        "streak=%s status=%s force_adb_refresh=%s",
                        capture_session_id,
                        attempt,
                        self._capture_transient_streak.get(capture_session_id, 0),
                        response.status_code,
                        int(force_refresh or attempt >= 0),
                    )
                await response.aclose()
                await _maybe_backoff_before_retry(
                    attempt=attempt,
                    enabled=retry_backoff,
                )
                continue
            if capture_session_id is not None and error.retryable:
                self._note_capture_transient_failure(capture_session_id)
            await response.aclose()
            raise error

        raise AssertionError("Official App 重试循环意外结束")

    def _note_capture_transient_failure(self, session_id: str) -> None:
        self._capture_transient_streak[session_id] = (
            self._capture_transient_streak.get(session_id, 0) + 1
        )

    def _clear_capture_transient_streak(self, session_id: str) -> None:
        self._capture_transient_streak.pop(session_id, None)

    def _session_handle(self, session_id: str) -> str:
        try:
            return self._session_handles[session_id]
        except KeyError as error:
            raise OfficialAppApiError("Official App 会话不存在或已经关闭") from error


class OfficialAppApiError(RuntimeError):
    """Official API 的网络、协议或私有会话错误。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "OFFICIAL_API_ERROR",
        retryable: bool = True,
        request_id: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.request_id = request_id
        self.http_status = http_status


def _is_action_outcome_unknown_error(error: OfficialAppApiError) -> bool:
    """判断动作请求是否可能已发出但没有可确认的设备结果。"""

    code = str(error.error_code or "").strip().upper()
    return bool(
        error.retryable
        or (error.http_status is not None and error.http_status >= 500)
        or code in {
            "OFFICIAL_API_TIMEOUT",
            "OFFICIAL_API_NETWORK",
            "OFFICIAL_RESPONSE_INVALID",
        }
        or code.startswith("OFFICIAL_HTTP_5")
    )


def _unknown_action_outcome_error(error: OfficialAppApiError) -> OfficialAppApiError:
    """把可重试的传输/服务错误收敛为不可重试的未知动作结果。"""

    return OfficialAppApiError(
        "Official App 动作结果未知，禁止自动重试",
        error_code=ACTION_OUTCOME_UNKNOWN_ERROR_CODE,
        retryable=False,
        request_id=error.request_id,
    )


def _is_valid_png_bytes(content: bytes) -> bool:
    """校验 PNG magic，避免损坏截图触发视觉 LLM 400 invalid_image。"""
    return (
        isinstance(content, bytes)
        and len(content) >= len(_PNG_MAGIC)
        and content.startswith(_PNG_MAGIC)
    )


async def _maybe_backoff_before_retry(*, attempt: int, enabled: bool) -> None:
    """截图等可重试故障在重试前短暂退避，避免云 ADB 瞬断时三连 502。"""
    if not enabled:
        return
    delay = min(
        _CAPTURE_RETRY_BACKOFF_BASE_SECONDS * (2 ** attempt),
        _CAPTURE_RETRY_BACKOFF_MAX_SECONDS,
    )
    await asyncio.sleep(delay)


def _validate_max_retries(value: int) -> int:
    """校验端点重试次数，拒绝布尔值、浮点数与负数。"""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("max_retries 必须是非负整数")
    if value < 0:
        raise ValueError("max_retries 不能小于 0")
    return value


def _capture_request_timeout(settings: WorkflowSettings) -> httpx.Timeout:
    """截图／UI Tree 专用超时：必须盖住云手机帧内最坏 pull／refresh 预算。"""

    configured = float(getattr(settings, "official_app_capture_timeout_seconds", 0) or 0)
    seconds = max(configured, _CAPTURE_TIMEOUT_FLOOR_SECONDS)
    return httpx.Timeout(seconds)


async def _read_limited_bytes(response: httpx.Response, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    received = 0
    async for chunk in response.aiter_bytes():
        received += len(chunk)
        if received > max_bytes:
            raise OfficialAppApiError(
                "Official 截图超过允许的最大字节数",
                error_code="CAPTURE_INVALID",
                retryable=False,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _json_mapping(response: httpx.Response) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise OfficialAppApiError(
            "Official API 返回的 JSON 非法",
            error_code="OFFICIAL_RESPONSE_INVALID",
            retryable=False,
        ) from error
    if not isinstance(payload, Mapping):
        raise OfficialAppApiError(
            "Official API 返回的 JSON 必须是对象",
            error_code="OFFICIAL_RESPONSE_INVALID",
            retryable=False,
        )
    return payload


def _parse_session_response(response: httpx.Response) -> OfficialSessionResponse:
    try:
        payload = _json_mapping(response)
        return OfficialSessionResponse(
            request_id=_required_text(payload, "request_id"),
            session_handle=_required_text(payload, "session_handle"),
        )
    except OfficialAppApiError as error:
        raise _response_contract_error(error) from error


def _parse_install_response(response: httpx.Response) -> OfficialInstallResponse:
    try:
        payload = _json_mapping(response)
        return OfficialInstallResponse(
            request_id=_required_text(payload, "request_id"),
            succeeded=_required_bool(payload, "succeeded"),
            retryable=_required_bool(payload, "retryable"),
            installed_package=_optional_text(payload, "installed_package"),
            reused_existing=bool(payload.get("reused_existing", False)),
            error_code=_optional_text(payload, "error_code"),
        )
    except OfficialAppApiError as error:
        raise _response_contract_error(error) from error


def _parse_open_response(response: httpx.Response) -> OfficialOpenResponse:
    try:
        payload = _json_mapping(response)
        return OfficialOpenResponse(
            request_id=_required_text(payload, "request_id"),
            succeeded=_required_bool(payload, "succeeded"),
            retryable=_required_bool(payload, "retryable"),
            package_name=_required_text(payload, "package_name"),
            foreground_verified=_required_bool(payload, "foreground_verified"),
            activity=_optional_text(payload, "activity"),
            error_code=_optional_text(payload, "error_code"),
        )
    except OfficialAppApiError as error:
        raise _response_contract_error(error) from error


def _parse_action_response(response: httpx.Response) -> OfficialActionResponse:
    try:
        payload = _json_mapping(response)
        action_trace = payload.get("action_trace", ())
        if not isinstance(action_trace, list) or any(
            not isinstance(item, Mapping) for item in action_trace
        ):
            raise OfficialAppApiError("Official action_trace 格式非法")
        return OfficialActionResponse(
            request_id=_required_text(payload, "request_id"),
            action_execution_id=_required_text(payload, "action_execution_id"),
            succeeded=_required_bool(payload, "succeeded"),
            retryable=_required_bool(payload, "retryable"),
            action_trace=tuple(dict(item) for item in action_trace),
            error_code=_optional_text(payload, "error_code"),
        )
    except OfficialAppApiError as error:
        raise _response_contract_error(error) from error


def _required_text(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise OfficialAppApiError(f"Official API 缺少 {field_name}")
    return value.strip()


def _validate_aux_payload_identity(
    payload: Mapping[str, Any],
    *,
    task_id: str,
    session_id: str,
    package_name: str | None,
) -> None:
    """约束 Official 辅助制品的最小状态与任务/会话/包身份。"""

    status = payload.get("status")
    if not isinstance(status, str) or not status.strip():
        raise OfficialAppApiError("Official capture-aux payload 缺少 status")
    for field_name, expected in (
        ("task_id", task_id),
        ("session_id", session_id),
        ("package_name", package_name),
    ):
        actual = payload.get(field_name)
        if expected is not None and actual is not None and actual != expected:
            raise OfficialAppApiError(
                f"Official capture-aux payload 的 {field_name} 身份不一致"
            )


def _reattach_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """恢复会话只透传业务选项，剥离本地 SurfaceSessionSpec 自动补齐字段。"""
    ignored = {
        "lifecycle_phase",
        "process_identity",
        "app_data_container_ref",
        "reset_policy",
    }
    return {key: value for key, value in dict(options).items() if key not in ignored}


def _optional_text(payload: Mapping[str, Any], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise OfficialAppApiError(f"Official API 的 {field_name} 非法")
    return value.strip()


def _response_contract_error(error: OfficialAppApiError) -> OfficialAppApiError:
    if error.error_code == "OFFICIAL_RESPONSE_INVALID":
        return error
    return OfficialAppApiError(
        "Official API 成功响应不符合合同",
        error_code="OFFICIAL_RESPONSE_INVALID",
        retryable=False,
    )


def _required_bool(payload: Mapping[str, Any], field_name: str) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise OfficialAppApiError(f"Official API 缺少布尔字段 {field_name}")
    return value


def _required_header_text(response: httpx.Response, name: str) -> str:
    value = response.headers.get(name)
    if not isinstance(value, str) or not value.strip():
        raise OfficialAppApiError(f"Official 截图缺少响应头 {name}")
    return value.strip()


def _optional_header_text(response: httpx.Response, name: str) -> str | None:
    value = response.headers.get(name)
    if value is None:
        return None
    return _required_header_text(response, name)


def _required_header_int(response: httpx.Response, name: str) -> int:
    value = _required_header_text(response, name)
    try:
        parsed = int(value)
    except ValueError as error:
        raise OfficialAppApiError(f"Official 截图响应头 {name} 必须是整数") from error
    if parsed <= 0:
        raise OfficialAppApiError(f"Official 截图响应头 {name} 必须大于 0")
    return parsed


def _required_orientation_header(response: httpx.Response) -> str:
    value = _required_header_text(response, "x-alioth-orientation")
    if value not in {"portrait", "landscape", "unknown"}:
        raise OfficialAppApiError("Official 截图响应头 x-alioth-orientation 非法")
    return value


def _transport_error(error: httpx.RequestError, *, timeout: bool) -> OfficialAppApiError:
    error_code = "OFFICIAL_API_TIMEOUT" if timeout else "OFFICIAL_API_NETWORK"
    _log_failure(error_code=error_code, request_id=None)
    return OfficialAppApiError(
        "Official API 请求超时" if timeout else "Official API 网络请求失败",
        error_code=error_code,
        retryable=True,
    )


def _http_status_error(response: httpx.Response) -> OfficialAppApiError:
    status_code = response.status_code
    error_code = f"OFFICIAL_HTTP_{status_code}"
    request_id = _optional_header_text(response, "x-request-id")
    retryable = status_code in _RETRYABLE_STATUS_CODES
    # 诊断用：保留上游业务 error_code／message，避免只剩 OFFICIAL_HTTP_403。
    upstream_code: str | None = None
    upstream_message: str | None = None
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        payload = None
    if isinstance(payload, Mapping):
        raw_code = payload.get("error_code")
        raw_message = payload.get("message")
        if isinstance(raw_code, str) and raw_code.strip():
            upstream_code = raw_code.strip()
            error_code = upstream_code
        if isinstance(raw_message, str) and raw_message.strip():
            upstream_message = raw_message.strip()[:200]
    _log_failure(
        error_code=error_code,
        request_id=request_id,
        http_status=status_code,
        upstream_error_code=upstream_code,
        upstream_message=upstream_message,
    )
    return OfficialAppApiError(
        upstream_message or "Official API 返回非成功状态",
        error_code=error_code,
        retryable=retryable,
        request_id=request_id,
        http_status=status_code,
    )


def _log_success(**fields: str | int | None) -> None:
    logger.info("official_app_api_succeeded", extra={"official_app_fields": _safe_fields(fields)})


def _log_failure(
    *,
    error_code: str,
    request_id: str | None,
    http_status: int | None = None,
    upstream_error_code: str | None = None,
    upstream_message: str | None = None,
) -> None:
    # Docker 日志偶发截断；同步写短行到挂载目录，便于对照 403 业务码。
    try:
        from pathlib import Path

        Path("/workspace/.tmp_official_api_fail.log").open("a", encoding="utf-8").write(
            f"error_code={error_code} http_status={http_status} "
            f"upstream={upstream_error_code} message={upstream_message} "
            f"request_id={request_id}\n"
        )
    except Exception:  # noqa: BLE001 - 诊断旁路不得影响主链
        pass
    logger.warning(
        "official_app_api_failed error_code=%s http_status=%s upstream=%s message=%s request_id=%s",
        error_code,
        http_status,
        upstream_error_code,
        upstream_message,
        request_id,
        extra={
            "official_app_fields": _safe_fields(
                {
                    "error_code": error_code,
                    "request_id": request_id,
                    "http_status": http_status,
                    "upstream_error_code": upstream_error_code,
                }
            )
        },
    )


def _safe_fields(fields: Mapping[str, str | int | None]) -> dict[str, str | int]:
    """只将白名单审计标识写入 Adapter 日志，拒绝凭据和私有句柄。"""
    return {
        name: value
        for name, value in fields.items()
        if name in _OBSERVABILITY_FIELDS and value is not None
    }
