"""Android ADB SurfaceDriver：持有裝置原語並執行 ActionIntent。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from services.api.app.application.interaction_runtime import (
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CapabilityDescriptor,
    CapabilityStatus,
    CaptureProfile,
    InteractionOperation,
    RawActionResult,
    RawArtifact,
    RawArtifactSet,
    RawSurfaceObservation,
    SurfaceCapabilityManifest,
    SurfaceDriverError,
    SurfaceKind,
    SurfaceSessionRef,
    SurfaceSessionSpec,
)
from services.api.app.infrastructure.device.adb_ops import (
    _CAPTURE_IN_FRAME_FAILURE_RETRIES,
    _CAPTURE_IN_FRAME_RETRY_BACKOFF_SECONDS,
    _SCREENSHOT_CLOUD_PHONE_RETRY_PULL_TIMEOUT_SECONDS,
    AdbDeviceOps,
    AppCapturePipelineLatency,
    _elapsed_ms,
    open_adb_device_ops,
)
from services.api.app.infrastructure.device.adb_session_health import (
    AdbSessionHealthState,
    merge_health_into_pipeline,
    recover_cloud_phone_adb_binding,
    run_with_cloud_phone_adb_reconnect,
)
from services.api.app.infrastructure.device.android_device_ops import AndroidDeviceOps
from services.api.app.infrastructure.device.android_runtime_observation import (
    AndroidRuntimeObservation,
    parse_android_ui_hierarchy,
)
from services.api.app.infrastructure.device.android_target_resolver import (
    CanonicalPointTargetResolver,
)
from services.api.app.infrastructure.device.app_tap_audit_log import log_app_tap_audit
from services.api.app.infrastructure.device.backend import BACKEND_CLOUD_PHONE

_LOGGER = logging.getLogger(__name__)
_HJLL_EXTERNAL_BACK_TOKENS = ("sbrowser", "chrome", "browser", "vending")


def _hjll_package_needs_cloud_phone_back(package_name: str) -> bool:
    blob = package_name.casefold()
    return any(token in blob for token in _HJLL_EXTERNAL_BACK_TOKENS)

_ADAPTER_NAME = "android-adb"
_ADAPTER_VERSION = "android-adb.v1"


class AndroidAdbSurfaceDriver:
    """App Driver Adapter（架構 §15 Android Adapter）；僅由 Interaction Runtime 持有。"""

    def __init__(
        self,
        *,
        backend: str,
        ld_index: int = 0,
        log: Callable[[str], None] | None = None,
        capture_dir: Path | None = None,
        ops: AndroidDeviceOps | AdbDeviceOps | None = None,
        cloud_phone_controller: object | None = None,
    ) -> None:
        self._backend = backend
        self._ld_index = ld_index
        self._log = log or (lambda _msg: None)
        self._capture_dir = capture_dir
        self._ops = ops
        self._cloud_phone_controller = cloud_phone_controller
        self._adb_session_health: AdbSessionHealthState | None = None
        self._resolver: CanonicalPointTargetResolver | None = None
        self._session: SurfaceSessionRef | None = None
        self._last_runtime_observation: AndroidRuntimeObservation | None = None
        # 單一 Owner：禁止同一 Driver 實例並行掛接第二套可寫 Ops。
        self._ops_owner_token: str | None = "injected" if ops is not None else None

    @property
    def ops(self) -> AndroidDeviceOps:
        """僅供 Adapter／Runtime 內部使用；呼叫端禁止經此直打裝置。"""
        return self._require_ops()

    def _require_ops(self) -> AndroidDeviceOps:
        if self._ops is None:
            raise RuntimeError("Android SurfaceDriver 尚未 start_session")
        return self._ops

    def bind_ops(self, ops: AndroidDeviceOps, *, owner_token: str) -> None:
        """显式绑定设备 Ops；同 Session 禁止双 Owner。"""
        if not owner_token or owner_token != owner_token.strip():
            raise ValueError("owner_token 必须为非空无首尾空白字符串")
        if self._ops is not None and self._ops_owner_token not in {None, owner_token}:
            raise RuntimeError("禁止双 Owner 对同一 Android Session 绑定设备 Ops")
        self._ops = ops
        self._ops_owner_token = owner_token

    def clear_ops(self, *, owner_token: str | None = None) -> None:
        if (
            owner_token is not None
            and self._ops_owner_token is not None
            and owner_token != self._ops_owner_token
        ):
            raise RuntimeError("非 Owner 不得清除 Android Session 设备 Ops")
        self._ops = None
        self._ops_owner_token = None

    @property
    def session(self) -> SurfaceSessionRef:
        if self._session is None:
            raise RuntimeError("Android SurfaceDriver 尚未 start_session")
        return self._session

    def describe_capabilities(self) -> SurfaceCapabilityManifest:
        ready = CapabilityDescriptor(status=CapabilityStatus.READY)
        return SurfaceCapabilityManifest(
            adapter_id=_ADAPTER_NAME,
            adapter_version=_ADAPTER_VERSION,
            surface_kind=SurfaceKind.ANDROID,
            platform="android",
            backends=("cloud_phone", "ldplayer", "google_emulator", "redroid"),
            actions={
                "session": ready,
                "close": ready,
                "tap": ready,
                "input": ready,
                "scroll": ready,
                "back": ready,
                "observe": ready,
                "timed_capture": ready,
                "warm_restore": ready,
                "deep_link": ready,
            },
            observations={
                "screenshot": ready,
                "ui_tree": CapabilityDescriptor(
                    status=CapabilityStatus.READY,
                    fields=(
                        "node_id",
                        "class_name",
                        "bounds",
                        "text",
                        "content_desc",
                        "visible_to_user",
                    ),
                    notes=(
                        "通过受控 uiautomator dump 获取；仅作为运行时事实，"
                        "不作为截图 Artifact 持久化"
                    ),
                ),
            },
            artifacts={
                "screenshot": ready,
                "device_log": ready,
                "ui_snapshot": ready,
            },
            features={
                "coordinate_space": "canonical_px",
                "canonical_size": {"width": 1080, "height": 1920},
            },
        )

    async def start_session(self, spec: SurfaceSessionSpec) -> SurfaceSessionRef:
        if spec.surface_kind is not SurfaceKind.ANDROID:
            raise ValueError("AndroidAdbSurfaceDriver 只支持 Android Surface")
        if self._backend == BACKEND_CLOUD_PHONE:
            self._adb_session_health = AdbSessionHealthState()
            if self._cloud_phone_controller is None and self._ops is None:
                from services.api.app.infrastructure.device.cloud_phone import (
                    CloudPhoneController,
                )

                self._cloud_phone_controller = CloudPhoneController()
        if self._ops is None:
            self._ops = await asyncio.to_thread(
                open_adb_device_ops,
                self._backend,
                ld_index=self._ld_index,
                log=self._log,
                cloud_phone_controller=self._cloud_phone_controller,
            )
            self._ops_owner_token = "session-start"
        self._resolver = CanonicalPointTargetResolver.from_device_ops(self._ops)
        await self._ensure_portrait_lock(where="session-start")
        self._session = SurfaceSessionRef(
            task_id=spec.task_id,
            session_id=spec.session_id,
            surface_kind=spec.surface_kind,
            primary_surface_ref=f"android-primary:{spec.session_id}",
        )
        self._log(
            f"android surface session started backend={self._backend} "
            f"size={self._ops.width}x{self._ops.height}"
        )
        return self._session

    async def _ensure_portrait_lock(self, *, where: str) -> None:
        """App 质检强制直屏。任何后端上 App／浏览器转横，下一帧截图和点击前压回。"""

        if self._ops is None:
            return
        try:
            logs = await asyncio.to_thread(self._ops.force_portrait_lock)
        except Exception as exc:  # noqa: BLE001 - 直屏锁失败不阻断质检
            _LOGGER.info(
                "android surface portrait lock skipped where=%s error=%s",
                where,
                type(exc).__name__,
            )
            return
        self._resolver = CanonicalPointTargetResolver.from_device_ops(self._ops)
        self._log(
            f"android surface portrait lock where={where} "
            + "; ".join(str(line) for line in logs)
        )

    async def settle_hjll_orchestrated_action(
        self,
        *,
        expected_package: str,
        settle_ms: object,
        recover_foreground: bool = True,
    ) -> dict[str, object]:
        """执行固定步骤后的等待及前台恢复，不重新选择任何页面控件。"""

        package_name = str(expected_package or "").strip()
        if not package_name:
            raise SurfaceDriverError(
                "编排动作缺少目标包名",
                error_code="HJLL_ORCHESTRATION_PACKAGE_MISSING",
                retryable=False,
            )
        delay_ms = (
            int(settle_ms)
            if isinstance(settle_ms, int) and not isinstance(settle_ms, bool)
            else 1_500
        )
        delay_ms = min(30_000, max(0, delay_ms))
        if delay_ms:
            await asyncio.sleep(delay_ms / 1_000)
        if not recover_foreground:
            return {
                "supported": True,
                "recovered": False,
                "package_name": package_name,
                "recovery": "hold",
            }

        identity_reader = getattr(self.ops, "current_runtime_identity", None)
        current = self._hjll_runtime_identity(identity_reader)
        if current is not None and str(current[0] or "").strip() == package_name:
            return {"supported": True, "recovered": False, "package_name": package_name}

        # 支付外跳到浏览器或 Play 商店时，monkey LAUNCHER 会把云手机打到桌面。
        # 只用云手机返回键回到 App，不唤起桌面启动器。
        if current is not None and _hjll_package_needs_cloud_phone_back(str(current[0] or "")):
            back = getattr(self.ops, "back", None)
            if not callable(back):
                raise SurfaceDriverError(
                    "HJLL 外跳浏览器或 Play 商店后无法发送云手机返回",
                    error_code="HJLL_ORCHESTRATION_FOREGROUND_RECOVERY_FAILED",
                    retryable=True,
                )
            back_count = 0
            for _ in range(3):
                await self._call_ops_with_cloud_phone_reconnect(lambda: back())
                back_count += 1
                await asyncio.sleep(1.2)
                current = self._hjll_runtime_identity(identity_reader)
                foreground = str(current[0] or "").strip() if current is not None else ""
                if foreground == package_name:
                    return {
                        "supported": True,
                        "recovered": True,
                        "package_name": package_name,
                        "recovery": "back",
                        "back_count": back_count,
                    }
                if not foreground or not _hjll_package_needs_cloud_phone_back(foreground):
                    break
            if current is not None and str(current[0] or "").strip() != package_name:
                raise SurfaceDriverError(
                    "HJLL 外跳浏览器或 Play 商店后云手机返回未能回到目标 App",
                    error_code="HJLL_ORCHESTRATION_FOREGROUND_RECOVERY_FAILED",
                    retryable=True,
                )
            return {
                "supported": True,
                "recovered": True,
                "package_name": package_name,
                "recovery": "back",
                "back_count": back_count,
            }

        # 非浏览器的外跳仍用热恢复把已有任务带回前台，不 force-stop。
        warmer = getattr(self.ops, "warm_bring_to_front", None)
        if not callable(warmer):
            return {
                "supported": True,
                "recovered": False,
                "foreground_check": "unavailable",
                "package_name": package_name,
            }
        await self._call_ops_with_cloud_phone_reconnect(
            lambda: warmer(package_name)
        )
        await asyncio.sleep(2.0)
        current = self._hjll_runtime_identity(identity_reader)
        if current is not None and str(current[0] or "").strip() != package_name:
            raise SurfaceDriverError(
                "HJLL 动作后未能恢复目标 App 前台",
                error_code="HJLL_ORCHESTRATION_FOREGROUND_RECOVERY_FAILED",
                retryable=True,
            )
        return {"supported": True, "recovered": True, "package_name": package_name}

    @staticmethod
    def _hjll_runtime_identity(identity_reader: object) -> tuple[str, str] | None:
        if not callable(identity_reader):
            return None
        try:
            identity = identity_reader()
        except Exception:  # noqa: BLE001 - 前台观察失败不能推翻已成功的触控
            return None
        if not isinstance(identity, tuple) or len(identity) < 2:
            return None
        return str(identity[0] or ""), str(identity[1] or "")

    @property
    def last_runtime_observation(self) -> AndroidRuntimeObservation | None:
        """最近一次设备 UI 层级观察，仅供当前 Runtime 会话内存使用。"""
        return self._last_runtime_observation

    async def reattach_session(self, spec: SurfaceSessionSpec) -> SurfaceSessionRef:
        if self._session is None:
            raise RuntimeError("Android SurfaceDriver 没有可恢复的设备会话")
        if self._session.session_id != spec.session_id or self._session.task_id != spec.task_id:
            raise RuntimeError("Android SurfaceDriver 会话身份不匹配")
        return self._session

    async def close_session(self, *, session: SurfaceSessionRef, reason: str) -> None:
        _require_android_session(session)
        self._log(f"android surface session close reason={reason}")
        self._session = None
        # 不關閉底層模擬器進程；僅釋放本 Adapter 綁定
        self._resolver = None
        self._last_runtime_observation = None
        self._adb_session_health = None

    async def _maybe_proactive_refresh_cloud_phone(self) -> None:
        """慢 pull／年龄／低分时预防性刷新 MoreLogin ADB（换通道≠exec-out）。"""

        health = self._adb_session_health
        controller = self._cloud_phone_controller
        if health is None or controller is None:
            return
        health.last_refreshed = False
        should, reason = health.should_proactive_refresh()
        if not should:
            return
        try:
            controller, _serial, ops, _logs = await asyncio.to_thread(
                recover_cloud_phone_adb_binding,
                controller=controller,
                log=self._log,
            )
        except Exception as exc:  # noqa: BLE001 - 预防刷新失败不阻断本帧截图
            _LOGGER.info(
                "android surface proactive adb refresh skipped reason=%s error=%s",
                reason,
                type(exc).__name__,
            )
            return
        self._cloud_phone_controller = controller
        self._ops = ops
        self._resolver = CanonicalPointTargetResolver.from_device_ops(ops)
        health.mark_refreshed(reason=reason or "proactive")
        self._log(
            f"android surface proactive adb refresh reason={reason} "
            f"refresh_count={health.refresh_count}"
        )
        _LOGGER.info(
            "android surface proactive adb refresh reason=%s refresh_count=%s score=%s",
            reason,
            health.refresh_count,
            health.score,
        )

    async def _screencap_with_cloud_phone_retry(self, dest: Path) -> None:
        """cloud_phone 截图失败时与 LiveCloudPhone Bridge 对齐：refresh + 再截，最多额外 N 次。"""

        try:
            await asyncio.to_thread(self.ops.screencap, dest)
            return
        except Exception as first_exc:
            if (
                self._backend != BACKEND_CLOUD_PHONE
                or self._cloud_phone_controller is None
            ):
                raise
            last_exc: BaseException = first_exc
            for retry_i in range(_CAPTURE_IN_FRAME_FAILURE_RETRIES):
                backoff_i = min(
                    retry_i, len(_CAPTURE_IN_FRAME_RETRY_BACKOFF_SECONDS) - 1
                )
                await asyncio.sleep(_CAPTURE_IN_FRAME_RETRY_BACKOFF_SECONDS[backoff_i])
                try:
                    controller, _serial, ops, _logs = await asyncio.to_thread(
                        recover_cloud_phone_adb_binding,
                        controller=self._cloud_phone_controller,
                        log=self._log,
                        pull_timeout_seconds=(
                            _SCREENSHOT_CLOUD_PHONE_RETRY_PULL_TIMEOUT_SECONDS
                        ),
                    )
                except Exception:
                    raise last_exc from None
                self._cloud_phone_controller = controller
                self._ops = ops
                self._resolver = CanonicalPointTargetResolver.from_device_ops(ops)
                if self._adb_session_health is not None:
                    self._adb_session_health.mark_refreshed(
                        reason=f"capture_failure_retry_{retry_i + 1}"
                    )
                self._log(
                    f"android surface capture failure refresh "
                    f"attempt={retry_i + 1}/{_CAPTURE_IN_FRAME_FAILURE_RETRIES}"
                )
                try:
                    await asyncio.to_thread(self.ops.screencap, dest)
                    return
                except Exception as retry_exc:  # noqa: BLE001
                    last_exc = retry_exc
                    _LOGGER.info(
                        "android surface capture in-frame retry failed "
                        "attempt=%s/%s error=%s",
                        retry_i + 1,
                        _CAPTURE_IN_FRAME_FAILURE_RETRIES,
                        type(retry_exc).__name__,
                    )
            raise last_exc from None

    def _reconnect_cloud_phone_adb_for_action(self) -> None:
        """动作／UI dump 瞬断后刷新 MoreLogin 绑定并重建 ops。"""

        if self._cloud_phone_controller is None:
            raise RuntimeError("云手机控制器不可用，无法重连 ADB")
        controller, _serial, ops, _logs = recover_cloud_phone_adb_binding(
            controller=self._cloud_phone_controller,
            log=self._log,
        )
        self._cloud_phone_controller = controller
        self._ops = ops
        self._resolver = CanonicalPointTargetResolver.from_device_ops(ops)
        if self._adb_session_health is not None:
            self._adb_session_health.mark_refreshed(reason="action_adb_reconnect")

    async def _call_ops_with_cloud_phone_reconnect(
        self, run: Callable[[], object]
    ) -> object:
        """cloud_phone 路径对 ADB 调用做有界重连；其他 backend 直接执行。"""

        if (
            self._backend != BACKEND_CLOUD_PHONE
            or self._cloud_phone_controller is None
        ):
            return await asyncio.to_thread(run)
        return await asyncio.to_thread(
            run_with_cloud_phone_adb_reconnect,
            run=run,
            reconnect=self._reconnect_cloud_phone_adb_for_action,
            log=self._log,
        )

    async def observe_runtime_state(
        self,
        *,
        explored_safe_tab_ids: set[str] | frozenset[str] = frozenset(),
    ) -> AndroidRuntimeObservation:
        """读取并解析当前设备 UI 层级；失败时返回不可用事实，禁止静默伪造。"""
        _require_android_session(self.session)
        width, height = self.ops.wm_size()
        try:
            raw_xml = await self._call_ops_with_cloud_phone_reconnect(
                lambda: self.ops.dump_ui_hierarchy()
            )
            observation = parse_android_ui_hierarchy(
                str(raw_xml),
                width=width,
                height=height,
                explored_safe_tab_ids=explored_safe_tab_ids,
            )
        except Exception as exc:  # noqa: BLE001 - 观察失败必须进入门禁事实
            observation = AndroidRuntimeObservation.unavailable(
                width=width,
                height=height,
                failure_reason=f"{type(exc).__name__}: {exc}",
            )
        self._last_runtime_observation = observation
        return observation

    async def capture_same_frame(
        self,
        *,
        session: SurfaceSessionRef,
        explored_safe_tab_ids: set[str] | frozenset[str] = frozenset(),
    ) -> dict[str, object]:
        """同会话紧邻截图＋UIA；共享 observation_epoch，并推断 surface_tech。"""

        _require_android_session(session)
        await self._maybe_proactive_refresh_cloud_phone()
        dest = self._next_capture_path(session)
        capture_fn = getattr(self.ops, "capture_same_frame_observation", None)
        if not callable(capture_fn):
            raise RuntimeError("当前 AndroidDeviceOps 未装配同帧观察能力")
        bundle = await self._call_ops_with_cloud_phone_reconnect(
            lambda: capture_fn(screenshot_dest=dest)
        )
        if not isinstance(bundle, dict):
            raise RuntimeError("同帧观察返回非对象")
        xml = bundle.get("ui_hierarchy_xml")
        width = int((bundle.get("display") or {}).get("width") or self.ops.width)
        height = int((bundle.get("display") or {}).get("height") or self.ops.height)
        if isinstance(xml, str) and "<hierarchy" in xml:
            observation = parse_android_ui_hierarchy(
                xml,
                width=width,
                height=height,
                explored_safe_tab_ids=explored_safe_tab_ids,
            )
            self._last_runtime_observation = observation
            bundle = {
                **bundle,
                "runtime_observation_available": True,
                "has_meaningful_content": observation.has_meaningful_content,
            }
        else:
            self._last_runtime_observation = AndroidRuntimeObservation.unavailable(
                width=width,
                height=height,
                failure_reason=str(bundle.get("ui_hierarchy_error") or "ui_missing"),
            )
            bundle = {**bundle, "runtime_observation_available": False}
        bundle["screenshot_artifact_path"] = str(dest)
        return bundle

    async def read_runtime_identity(
        self,
        *,
        session: SurfaceSessionRef,
    ) -> RawSurfaceObservation:
        """只读前台包名／Activity，不截图、不传全图。"""

        _require_android_session(session)
        runtime_identity = await self._call_ops_with_cloud_phone_reconnect(
            lambda: self.ops.current_runtime_identity()
        )
        identity_payload: dict[str, object] = {"identity_source": "dumpsys"}
        if isinstance(runtime_identity, tuple) and runtime_identity:
            package_name, activity = runtime_identity[0], runtime_identity[1]
            if isinstance(package_name, str) and package_name.strip():
                identity_payload["package_name"] = package_name
            if isinstance(activity, str) and activity.strip():
                identity_payload["activity"] = activity
        return RawSurfaceObservation(
            task_id=session.task_id,
            session_id=session.session_id,
            surface_kind=session.surface_kind,
            surface_ref=session.primary_surface_ref,
            payload=identity_payload,
        )

    async def observe(
        self,
        *,
        session: SurfaceSessionRef,
        profile: CaptureProfile,
    ) -> RawSurfaceObservation:
        _require_android_session(session)
        artifacts = await self.capture(session=session, profile=profile)
        width, height = self.ops.wm_size()
        capture_metadata = dict(artifacts.metadata)
        identity_payload = {
            key: capture_metadata[key]
            for key in ("package_name", "activity")
            if key in capture_metadata
        }
        return RawSurfaceObservation(
            task_id=session.task_id,
            session_id=session.session_id,
            surface_kind=session.surface_kind,
            surface_ref=session.primary_surface_ref,
            payload={
                "artifact_count": len(artifacts.artifacts),
                "display": {"width": width, "height": height},
                "backend": self._backend,
                **identity_payload,
            },
        )

    async def capture(
        self,
        *,
        session: SurfaceSessionRef,
        profile: CaptureProfile,
    ) -> RawArtifactSet:
        del profile
        _require_android_session(session)
        await self._maybe_proactive_refresh_cloud_phone()
        await self._ensure_portrait_lock(where="capture")
        pipeline_started = time.perf_counter()
        captured_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        dest = self._next_capture_path(session)
        await self._screencap_with_cloud_phone_retry(dest)
        screencap_trace = self.ops.last_screencap_trace
        if self._adb_session_health is not None:
            self._adb_session_health.record_pull(
                screencap_trace.adb_pull_ms if screencap_trace else None
            )
        png = dest.read_bytes()
        wm_started = time.perf_counter()
        (width, height), wm_source = await asyncio.to_thread(self.ops.query_wm_size)
        wm_size_ms = _elapsed_ms(wm_started)
        identity_started = time.perf_counter()
        runtime_identity = await asyncio.to_thread(self.ops.current_runtime_identity)
        identity_ms = _elapsed_ms(identity_started)
        identity_metadata = (
            {
                "package_name": runtime_identity[0],
                "activity": runtime_identity[1],
            }
            if runtime_identity is not None
            else {}
        )
        pipeline = AppCapturePipelineLatency(
            captured_at=captured_at,
            path_mode=screencap_trace.path_mode if screencap_trace else "unknown",
            remote_screencap_ms=(
                screencap_trace.remote_screencap_ms if screencap_trace else None
            ),
            adb_pull_ms=screencap_trace.adb_pull_ms if screencap_trace else None,
            cleanup_ms=screencap_trace.cleanup_ms if screencap_trace else None,
            exec_out_ms=screencap_trace.exec_out_ms if screencap_trace else None,
            wm_size_ms=wm_size_ms,
            wm_size_source=wm_source,
            identity_ms=identity_ms,
            png_bytes=len(png),
            total_ms=_elapsed_ms(pipeline_started),
            error_code=screencap_trace.error_code if screencap_trace else None,
        )
        pipeline_payload = merge_health_into_pipeline(
            pipeline.to_dict(),
            self._adb_session_health,
        )
        health_audit = pipeline_payload.get("adb_session_health")
        _LOGGER.info(
            "App capture pipeline latency path_mode=%s remote_screencap_ms=%s "
            "adb_pull_ms=%s cleanup_ms=%s wm_size_ms=%s identity_ms=%s png_bytes=%s "
            "total_ms=%s backend=%s adb_health_score=%s adb_refreshed=%s",
            pipeline.path_mode,
            pipeline.remote_screencap_ms,
            pipeline.adb_pull_ms,
            pipeline.cleanup_ms,
            pipeline.wm_size_ms,
            pipeline.identity_ms,
            pipeline.png_bytes,
            pipeline.total_ms,
            self._backend,
            None if not isinstance(health_audit, dict) else health_audit.get("score"),
            None if not isinstance(health_audit, dict) else health_audit.get("refreshed"),
        )
        return RawArtifactSet(
            task_id=session.task_id,
            session_id=session.session_id,
            surface_ref=session.primary_surface_ref,
            artifacts=(RawArtifact(artifact_type="screenshot", content=png),),
            metadata={
                "path": str(dest),
                "display": {"width": width, "height": height},
                "backend": self._backend,
                "capture_pipeline_latency": pipeline_payload,
                **identity_metadata,
            },
        )

    async def execute(
        self,
        *,
        session: SurfaceSessionRef,
        intent: ActionIntent,
        action_execution_id: str,
    ) -> RawActionResult:
        _require_android_session(session)
        from services.api.app.application.app_product_orchestration_policy import (
            is_fixed_script_parameters,
        )

        if is_fixed_script_parameters(intent.parameters):
            return RawActionResult(
                action_execution_id=action_execution_id, succeeded=False, retryable=False,
                error_code="LEGACY_PRODUCT_SCRIPT_DISABLED", payload={},
            )
        if (
            intent.task_id != session.task_id
            or intent.session_id != session.session_id
            or intent.surface_kind is not SurfaceKind.ANDROID
        ):
            return RawActionResult(
                action_execution_id=action_execution_id,
                succeeded=False,
                retryable=False,
                error_code="ACTION_SESSION_MISMATCH",
                payload={},
            )
        if self._resolver is None:
            return RawActionResult(
                action_execution_id=action_execution_id,
                succeeded=False,
                retryable=True,
                error_code="SURFACE_DRIVER_NOT_READY",
                payload={},
            )
        try:
            # 青雲 Explorer 发 CLICK + resolved_target；星语/本机 ADB 统一为 TAP + point
            from services.api.app.application.xingyu_runtime.action_normalize import (
                normalize_action_type,
                normalize_execute_parameters,
            )

            op_name = normalize_action_type(intent.action_type.value)
            try:
                action_type = InteractionOperation(op_name)
            except ValueError:
                return RawActionResult(
                    action_execution_id=action_execution_id,
                    succeeded=False,
                    retryable=False,
                    error_code="ANDROID_OPERATION_UNSUPPORTED",
                    payload={"action_type": intent.action_type.value},
                )
            parameters = normalize_execute_parameters(
                dict(intent.parameters) if intent.parameters else {}
            )
            await self._ensure_portrait_lock(where="execute")
            # 横竖屏切换后刷新 wm／resolver，避免 Physical 竖屏尺寸校验横屏 UIA 落点。
            try:
                self.ops.query_wm_size()
                self._resolver = CanonicalPointTargetResolver.from_device_ops(self.ops)
            except Exception:  # noqa: BLE001 - 刷新失败仍用既有 resolver
                pass
            resolved = self._resolver.resolve_android(
                SemanticTargetResolutionRequest(
                    task_id=intent.task_id,
                    session_id=intent.session_id,
                    source_capture_id=intent.source_capture_id,
                    action_type=action_type,
                    semantic_target=intent.semantic_target,
                    parameters=parameters,
                ),
                runtime_observation=self._last_runtime_observation,
            )
        except SemanticTargetResolutionError as exc:
            return RawActionResult(
                action_execution_id=action_execution_id,
                succeeded=False,
                retryable=exc.retryable,
                error_code=exc.error_code,
                payload={"message": str(exc)},
            )

        try:
            if resolved.operation is InteractionOperation.TAP:
                log_app_tap_audit(
                    "driver_execute",
                    action_execution_id=action_execution_id,
                    session_id=session.session_id,
                    task_id=intent.task_id,
                    semantic_target=intent.semantic_target,
                    source_capture_id=intent.source_capture_id,
                    device_x=resolved.tap_x,
                    device_y=resolved.tap_y,
                    resolution_method=resolved.resolution_method,
                    mapping_audit=resolved.mapping_audit,
                    backend=getattr(self.ops, "backend", None),
                )
                await self._call_ops_with_cloud_phone_reconnect(
                    lambda: self.ops.tap(resolved.tap_x, resolved.tap_y)
                )
                payload = {
                    "tap": {
                        "x": resolved.tap_x,
                        "y": resolved.tap_y,
                        "resolution_method": resolved.resolution_method,
                        "mapping_audit": dict(resolved.mapping_audit or {}),
                    }
                }
            elif resolved.operation is InteractionOperation.SCROLL:
                await self._call_ops_with_cloud_phone_reconnect(
                    lambda: self.ops.swipe(
                        resolved.swipe_x1,
                        resolved.swipe_y1,
                        resolved.swipe_x2,
                        resolved.swipe_y2,
                        ms=resolved.swipe_ms,
                    )
                )
                payload = {
                    "swipe": {
                        "x1": resolved.swipe_x1,
                        "y1": resolved.swipe_y1,
                        "x2": resolved.swipe_x2,
                        "y2": resolved.swipe_y2,
                        "ms": resolved.swipe_ms,
                    }
                }
            elif resolved.operation is InteractionOperation.BACK:
                package_name = str(
                    (intent.parameters or {}).get("package_name")
                    or getattr(session, "package_name", None)
                    or ""
                )
                cloud_back = _resolve_product_cloud_back_tap(
                    semantic_target=str(intent.semantic_target or ""),
                    purpose=str(
                        getattr(intent, "purpose", None)
                        or (intent.parameters or {}).get("purpose")
                        or parameters.get("purpose")
                        or ""
                    ),
                    package_name=package_name,
                )
                if cloud_back is not None:
                    bx, by = cloud_back
                    restored_after_desktop = False

                    def _double_cloud_back(
                        tap_x: int = bx,
                        tap_y: int = by,
                        target_pkg: str = package_name,
                    ) -> None:
                        nonlocal restored_after_desktop
                        # cascade D 档：底栏「<」连点两下；中间禁止截图。
                        _tap_cloud_back_twice_on_ops(self.ops, tap_x, tap_y)
                        # 连点可能打穿根页退桌面：立刻热拉回目标包。
                        pkg = str(target_pkg or "").strip()
                        if not pkg:
                            return
                        identity_fn = getattr(
                            self.ops, "current_runtime_identity", None
                        )
                        warm_fn = getattr(self.ops, "warm_bring_to_front", None)
                        if not callable(identity_fn) or not callable(warm_fn):
                            return
                        try:
                            identity = identity_fn()
                        except Exception:  # noqa: BLE001
                            identity = None
                        foreground = ""
                        if isinstance(identity, tuple) and identity:
                            foreground = str(identity[0] or "").strip()
                        if foreground.casefold() == pkg.casefold():
                            return
                        try:
                            warm_fn(pkg)
                            restored_after_desktop = True
                        except Exception:  # noqa: BLE001
                            pass

                    await self._call_ops_with_cloud_phone_reconnect(_double_cloud_back)
                    payload = {
                        "back": True,
                        "cloud_back_tap": {"x": bx, "y": by},
                        "cloud_back_count": 2,
                        "resolution_method": "product.cloud_back_tap_twice",
                        "restored_after_desktop": restored_after_desktop,
                    }
                else:
                    await self._call_ops_with_cloud_phone_reconnect(
                        lambda: self.ops.back()
                    )
                    payload = {"back": True}
            elif resolved.operation is InteractionOperation.INPUT:
                if resolved.tap_x is not None and resolved.tap_y is not None:
                    await self._call_ops_with_cloud_phone_reconnect(
                        lambda: self.ops.tap(resolved.tap_x, resolved.tap_y)
                    )
                await self._call_ops_with_cloud_phone_reconnect(
                    lambda: self.ops.type_text(resolved.input_text or "")
                )
                payload = {
                    "input": {
                        "text_len": len(resolved.input_text or ""),
                        "focused_tap": {
                            "x": resolved.tap_x,
                            "y": resolved.tap_y,
                        }
                        if resolved.tap_x is not None
                        else None,
                    }
                }
            elif resolved.operation in {
                InteractionOperation.OBSERVE,
                InteractionOperation.TIMED_CAPTURE,
            }:
                payload = {"observe": True}
            else:
                return RawActionResult(
                    action_execution_id=action_execution_id,
                    succeeded=False,
                    retryable=False,
                    error_code="ANDROID_OPERATION_UNSUPPORTED",
                    payload={},
                )
        except Exception as exc:  # noqa: BLE001 - Driver 異常轉結構化結果
            return RawActionResult(
                action_execution_id=action_execution_id,
                succeeded=False,
                retryable=True,
                error_code="SURFACE_DRIVER_FAILED",
                payload={"message": f"{type(exc).__name__}: {exc}"},
            )

        return RawActionResult(
            action_execution_id=action_execution_id,
            succeeded=True,
            retryable=False,
            payload=payload,
        )

    async def _execute_51pc_recording_script(
        self,
        *,
        action_execution_id: str,
    ) -> RawActionResult:
        """整份跑 51pc.py。不經視覺重選點。"""

        from services.api.app.application.app_51pc_script import (
            run_51pc_recording_script,
            serial_from_android_driver,
        )

        try:
            serial = serial_from_android_driver(self)
            recording = await asyncio.to_thread(
                run_51pc_recording_script,
                serial=serial,
            )
        except Exception as exc:  # noqa: BLE001 - 缺裝置或啟動失敗要留在這一步
            return RawActionResult(
                action_execution_id=action_execution_id,
                succeeded=False,
                retryable=False,
                error_code="PC51_SCRIPT_FAILED",
                payload={"message": f"{type(exc).__name__}: {exc}"},
            )
        ok = recording.get("ok") is True
        error_code = recording.get("error_code")
        return RawActionResult(
            action_execution_id=action_execution_id,
            succeeded=ok,
            retryable=False,
            error_code=None if ok else str(error_code or "PC51_SCRIPT_FAILED"),
            payload={"pc51_recording": recording},
        )

    async def _execute_hlwwhite_recording_script(
        self,
        *,
        action_execution_id: str,
    ) -> RawActionResult:
        """整份跑 hlwwhite.py。不經視覺重選點。"""

        from services.api.app.application.app_hlwwhite_script import (
            run_hlwwhite_recording_script,
            serial_from_android_driver,
        )

        try:
            serial = serial_from_android_driver(self)
            recording = await asyncio.to_thread(
                run_hlwwhite_recording_script,
                serial=serial,
            )
        except Exception as exc:  # noqa: BLE001 - 缺裝置或啟動失敗要留在這一步
            return RawActionResult(
                action_execution_id=action_execution_id,
                succeeded=False,
                retryable=False,
                error_code="HLWWHITE_SCRIPT_FAILED",
                payload={"message": f"{type(exc).__name__}: {exc}"},
            )
        ok = recording.get("ok") is True
        error_code = recording.get("error_code")
        return RawActionResult(
            action_execution_id=action_execution_id,
            succeeded=ok,
            retryable=False,
            error_code=None if ok else str(error_code or "HLWWHITE_SCRIPT_FAILED"),
            payload={"hlwwhite_recording": recording},
        )

    async def _execute_pzhan_recording_script(
        self,
        *,
        action_execution_id: str,
    ) -> RawActionResult:
        """整份跑 P.py。不經視覺重選點。"""

        from services.api.app.application.app_pzhan_script import (
            run_pzhan_recording_script,
            serial_from_android_driver,
        )

        try:
            serial = serial_from_android_driver(self)
            recording = await asyncio.to_thread(
                run_pzhan_recording_script,
                serial=serial,
            )
        except Exception as exc:  # noqa: BLE001 - 缺裝置或啟動失敗要留在這一步
            return RawActionResult(
                action_execution_id=action_execution_id,
                succeeded=False,
                retryable=False,
                error_code="PZHAN_SCRIPT_FAILED",
                payload={"message": f"{type(exc).__name__}: {exc}"},
            )
        ok = recording.get("ok") is True
        error_code = recording.get("error_code")
        return RawActionResult(
            action_execution_id=action_execution_id,
            succeeded=ok,
            retryable=False,
            error_code=None if ok else str(error_code or "PZHAN_SCRIPT_FAILED"),
            payload={"pzhan_recording": recording},
        )

    async def _execute_yms_recording_script(
        self,
        *,
        action_execution_id: str,
    ) -> RawActionResult:
        """整份跑 yms.py。不經視覺重選點。"""

        from services.api.app.application.app_yms_script import (
            run_yms_recording_script,
            serial_from_android_driver,
        )

        try:
            serial = serial_from_android_driver(self)
            recording = await asyncio.to_thread(
                run_yms_recording_script,
                serial=serial,
            )
        except Exception as exc:  # noqa: BLE001 - 缺裝置或啟動失敗要留在這一步
            return RawActionResult(
                action_execution_id=action_execution_id,
                succeeded=False,
                retryable=False,
                error_code="YMS_SCRIPT_FAILED",
                payload={"message": f"{type(exc).__name__}: {exc}"},
            )
        ok, error_code = _recording_outcome(recording, fallback="YMS_SCRIPT_FAILED")
        return RawActionResult(
            action_execution_id=action_execution_id,
            succeeded=ok,
            retryable=False,
            error_code=error_code,
            payload={"yms_recording": recording},
        )

    async def _execute_jinguo_recording_script(
        self,
        *,
        action_execution_id: str,
    ) -> RawActionResult:
        """整份跑 jinguo.py。不經視覺重選點。"""

        from services.api.app.application.app_jinguo_script import (
            run_jinguo_recording_script,
            serial_from_android_driver,
        )

        try:
            serial = serial_from_android_driver(self)
            recording = await asyncio.to_thread(
                run_jinguo_recording_script,
                serial=serial,
            )
        except Exception as exc:  # noqa: BLE001 - 缺裝置或啟動失敗要留在這一步
            return RawActionResult(
                action_execution_id=action_execution_id,
                succeeded=False,
                retryable=False,
                error_code="JINGUO_SCRIPT_FAILED",
                payload={"message": f"{type(exc).__name__}: {exc}"},
            )
        ok, error_code = _recording_outcome(recording, fallback="JINGUO_SCRIPT_FAILED")
        return RawActionResult(
            action_execution_id=action_execution_id,
            succeeded=ok,
            retryable=False,
            error_code=error_code,
            payload={"jinguo_recording": recording},
        )

    async def _execute_mama_recording_script(
        self,
        *,
        action_execution_id: str,
    ) -> RawActionResult:
        """整份跑 mama.py。不經視覺重選點。"""

        from services.api.app.application.app_mama_script import (
            run_mama_recording_script,
            serial_from_android_driver,
        )

        try:
            serial = serial_from_android_driver(self)
            recording = await asyncio.to_thread(
                run_mama_recording_script,
                serial=serial,
            )
        except Exception as exc:  # noqa: BLE001 - 缺裝置或啟動失敗要留在這一步
            return RawActionResult(
                action_execution_id=action_execution_id,
                succeeded=False,
                retryable=False,
                error_code="MAMA_SCRIPT_FAILED",
                payload={"message": f"{type(exc).__name__}: {exc}"},
            )
        ok, error_code = _recording_outcome(recording, fallback="MAMA_SCRIPT_FAILED")
        return RawActionResult(
            action_execution_id=action_execution_id,
            succeeded=ok,
            retryable=False,
            error_code=error_code,
            payload={"mama_recording": recording},
        )

    async def _execute_jmcomic_recording_script(
        self,
        *,
        action_execution_id: str,
    ) -> RawActionResult:
        """整份跑 jmcomic.py。不經視覺重選點。"""

        from services.api.app.application.app_jmcomic_script import (
            run_jmcomic_recording_script,
            serial_from_android_driver,
        )

        try:
            serial = serial_from_android_driver(self)
            recording = await asyncio.to_thread(
                run_jmcomic_recording_script,
                serial=serial,
            )
        except Exception as exc:  # noqa: BLE001 - 缺裝置或啟動失敗要留在這一步
            return RawActionResult(
                action_execution_id=action_execution_id,
                succeeded=False,
                retryable=False,
                error_code="JMCOMIC_SCRIPT_FAILED",
                payload={"message": f"{type(exc).__name__}: {exc}"},
            )
        ok, error_code = _recording_outcome(recording, fallback="JMCOMIC_SCRIPT_FAILED")
        return RawActionResult(
            action_execution_id=action_execution_id,
            succeeded=ok,
            retryable=False,
            error_code=error_code,
            payload={"jmcomic_recording": recording},
        )

    async def _execute_head_recording_script(
        self,
        *,
        action_execution_id: str,
    ) -> RawActionResult:
        """整份跑 head.py。不經視覺重選點。"""

        from services.api.app.application.app_head_script import (
            run_head_recording_script,
            serial_from_android_driver,
        )

        try:
            serial = serial_from_android_driver(self)
            recording = await asyncio.to_thread(
                run_head_recording_script,
                serial=serial,
            )
        except Exception as exc:  # noqa: BLE001 - 缺裝置或啟動失敗要留在這一步
            return RawActionResult(
                action_execution_id=action_execution_id,
                succeeded=False,
                retryable=False,
                error_code="HEAD_SCRIPT_FAILED",
                payload={"message": f"{type(exc).__name__}: {exc}"},
            )
        ok, error_code = _recording_outcome(recording, fallback="HEAD_SCRIPT_FAILED")
        return RawActionResult(
            action_execution_id=action_execution_id,
            succeeded=ok,
            retryable=False,
            error_code=error_code,
            payload={"head_recording": recording},
        )

    def _next_capture_path(self, session: SurfaceSessionRef) -> Path:
        base = self._capture_dir or Path.cwd() / ".alioth-android-captures"
        # Windows 路徑不可含 :<>"|?* 等字元；session_id 常帶命名空間冒號
        safe_id = "".join(
            ch if ch not in '<>:"/\\|?*' else "_" for ch in session.session_id
        ).strip(" ._") or "session"
        folder = base / safe_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"cap_{uuid4().hex[:12]}.png"


def _tap_cloud_back_once_on_ops(ops: object, x: int, y: int) -> None:
    """弹窗 cascade 兜底：同一坐标单次点底栏「<」。"""

    tap = getattr(ops, "tap", None)
    if callable(tap):
        tap(int(x), int(y))
        return
    shell_fn = getattr(ops, "_shell_fn", None)
    if not callable(shell_fn):
        raise RuntimeError("AndroidDeviceOps 缺少 tap")
    shell_fn(["input", "tap", str(int(x)), str(int(y))], 20.0)


def _tap_cloud_back_twice_on_ops(ops: object, x: int, y: int) -> None:
    """遗留：同一坐标连点两下（产品弹窗路径已改单次；工具层仍可调用）。

    优先 ``ops.tap`` 两次（云手机直播可见；参数列表安全）。
    """

    tap = getattr(ops, "tap", None)
    if callable(tap):
        tap(int(x), int(y))
        tap(int(x), int(y))
        return
    shell_fn = getattr(ops, "_shell_fn", None)
    if not callable(shell_fn):
        raise RuntimeError("AndroidDeviceOps 缺少 tap")
    shell_fn(
        ["sh", "-c", f"input tap {int(x)} {int(y)}; input tap {int(x)} {int(y)}"],
        30.0,
    )


def _resolve_product_cloud_back_tap(
    *,
    semantic_target: str,
    purpose: str,
    package_name: str,
) -> tuple[int, int] | None:
    """弹窗关层：产品策略要求点底栏最右侧「<」，禁止 keyevent 4。

    开屏／包边界恢复等仍走系统 BACK（返回 None）。
    """

    joined = f"{semantic_target} {purpose}".casefold()
    explicit_cloud_back = any(
        marker in joined
        for marker in (
            "底部导航栏",
            "cloud_back_tap",
            "cloud_back",
        )
    )
    # 开屏／包外恢复禁止 cloud_back，改走系统 keyevent。
    # 注意：弹窗 purpose 常写「禁止…开屏进入／浏览器…」——不得用裸「开屏／浏览器」
    # 子串误伤；显式 cloud_back／底栏「<」必须点坐标，禁止退化成 keyevent 4 退桌面。
    if not explicit_cloud_back and any(
        marker in joined
        for marker in (
            "开屏进入：",
            "开屏跳过",
            "点开屏",
            "首屏广告",
            "startup_ad",
            "tap_skip",
            "进入/跳过",
            "误进",
            "外部 activity",
            "浏览器",
            "应用商店",
            "系统桌面",
        )
    ):
        return None
    if not explicit_cloud_back and not any(
        marker in joined
        for marker in (
            "关闭弹窗",
            "关闭弹层",
            "弹窗广告",
            "遮挡弹窗",
            "遮挡弹层",
        )
    ):
        return None
    default_tap = (1120, 2955)
    try:
        from services.api.app.domain.app_product.profiles import load_profile

        profile = load_profile(package_name) if package_name.strip() else None
        if profile is not None:
            return profile.cloud_back_tap
        return default_tap
    except Exception:  # noqa: BLE001
        return default_tap


def _recording_outcome(
    recording: object,
    *,
    fallback: str,
) -> tuple[bool, str | None]:
    """退出碼成功還不夠。必備截圖沒齊就不能把這一步記成完成。"""

    if not isinstance(recording, dict):
        return False, fallback
    verification = recording.get("verification")
    verified = isinstance(verification, dict) and verification.get("verified") is True
    if recording.get("ok") is True and verified:
        return True, None
    error_code = recording.get("error_code")
    if recording.get("ok") is True and not verified:
        error_code = error_code or f"{fallback.removesuffix('_FAILED')}_EVIDENCE_MISSING"
    if not isinstance(error_code, str) or not error_code.strip():
        error_code = fallback
    return False, error_code


def _require_android_session(session: SurfaceSessionRef) -> None:
    if session.surface_kind is not SurfaceKind.ANDROID:
        raise ValueError("session.surface_kind 必须为 android")


# 架構文件 §15：App Driver Adapter 別名（與 PlaywrightSurfaceDriverAdapter 對稱）
AppSurfaceDriverAdapter = AndroidAdbSurfaceDriver
