"""App 会话级网络代理：设备 HTTP proxy + 有界 HAR 级 network_trace。

未启用代理时仍可回退 dumpsys 摘要（partial）；启用后 source=session_proxy。
"""

from __future__ import annotations

import logging
import socket
import threading
from collections import deque
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from services.api.app.application.app_surface_evidence import bound_network_trace_payload

logger = logging.getLogger(__name__)

_MAX_ENTRIES_PER_SESSION = 200
_DEFAULT_PROXY_HOST = "0.0.0.0"
_DEFAULT_PROXY_PORT = 18888


@dataclass(frozen=True, kw_only=True)
class AppNetworkProxySettings:
    """App 网络代理部署配置。"""

    enabled: bool = False
    listen_host: str = _DEFAULT_PROXY_HOST
    listen_port: int = _DEFAULT_PROXY_PORT
    device_proxy_host: str | None = None
    # 设备侧看到的代理地址；默认取 10.0.2.2（模拟器宿主机）或显式配置
    advertise_host: str = "10.0.2.2"
    # TLS MITM：解密 HTTPS 请求行；需设备信任 CA；失败时降级 CONNECT-only
    mitm_enabled: bool = False
    mitm_ca_dir: str | None = None
    # 弱网塑形：会话代理侧延迟（毫秒）；非设备 tc，审计须标 throttle_kind=session_http_proxy
    shape_latency_ms: int = 0

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> AppNetworkProxySettings:
        raw = dict(values or {})
        enabled = bool(raw.get("enabled", False))
        port = int(raw.get("listen_port") or _DEFAULT_PROXY_PORT)
        if port <= 0 or port > 65535:
            raise ValueError("listen_port 非法")
        host = str(raw.get("listen_host") or _DEFAULT_PROXY_HOST).strip() or _DEFAULT_PROXY_HOST
        advertise = str(raw.get("advertise_host") or "10.0.2.2").strip() or "10.0.2.2"
        device_host = raw.get("device_proxy_host")
        mitm_dir = raw.get("mitm_ca_dir")
        latency = int(raw.get("shape_latency_ms") or 0)
        if latency < 0 or latency > 10_000:
            raise ValueError("shape_latency_ms 非法")
        return cls(
            enabled=enabled,
            listen_host=host,
            listen_port=port,
            device_proxy_host=str(device_host).strip() if device_host else None,
            advertise_host=advertise,
            mitm_enabled=bool(raw.get("mitm_enabled", False)),
            mitm_ca_dir=str(mitm_dir).strip() if mitm_dir else None,
            shape_latency_ms=latency,
        )

    def endpoint_ref(self) -> str:
        host = self.device_proxy_host or self.advertise_host
        return f"{host}:{self.listen_port}"


@dataclass
class _SessionBuffer:
    entries: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_MAX_ENTRIES_PER_SESSION)
    )
    bound_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    proxy_endpoint: str | None = None


class AppNetworkProxyHub:
    """进程内会话缓冲 + 可选本地录音代理。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: MutableMapping[str, _SessionBuffer] = {}
        self._server: _RecordingProxyServer | None = None
        self._settings: AppNetworkProxySettings | None = None
        self._ca_install_by_session: MutableMapping[str, dict[str, Any]] = {}
        self._last_ca_install: dict[str, Any] | None = None
        self._shape_latency_ms = 0
        self._fault_mode = "none"

    @property
    def settings(self) -> AppNetworkProxySettings | None:
        return self._settings

    def configure(self, settings: AppNetworkProxySettings) -> None:
        with self._lock:
            self._settings = settings
            if settings.shape_latency_ms > 0:
                self._shape_latency_ms = int(settings.shape_latency_ms)
            if self._server is not None:
                self._server.set_shape_latency_ms(self._shape_latency_ms)

    def apply_weak_network_shape(self, *, latency_ms: int = 800) -> dict[str, Any]:
        """对已启动的会话代理施加连接延迟塑形（非设备 tc）。

        仅当代理存活且 latency>0 时 ``network_throttle_applied=true``；
        未挂代理的 App 流量不受影响，不得冒充全机限速。
        """

        latency = max(0, min(int(latency_ms), 10_000))
        with self._lock:
            self._shape_latency_ms = latency
            if self._server is not None:
                self._server.set_shape_latency_ms(latency)
            applied = bool(
                self._server is not None and self._server.alive and latency > 0
            )
            return {
                "enabled": applied,
                "latency_ms": latency,
                "throttle_kind": "session_http_proxy" if applied else None,
                "network_throttle_applied": applied,
                "scope": "http_proxy_traffic_only",
            }

    def apply_network_fault(self, mode: str = "offline") -> dict[str, Any]:
        """对已经在听的会话代理注入断网或超时。代理没起来时不算成功。"""

        requested = str(mode or "offline").strip().casefold()
        if requested not in {"offline", "timeout", "none"}:
            requested = "offline"
        with self._lock:
            alive = self._server is not None and self._server.alive
            if alive and self._server is not None:
                self._server.set_fault_mode(requested)
                self._fault_mode = requested
            else:
                self._fault_mode = "none"
            applied = bool(alive and requested in {"offline", "timeout"})
            return {
                "supported": applied,
                "result": (
                    "network_fault_injected"
                    if applied
                    else "network_fault_unsupported_on_app"
                ),
                "evidence_status": "captured" if applied else "evidence_insufficient",
                "fault_mode": requested if applied else "none",
                "scope": "http_proxy_traffic_only",
                "reason": (
                    None
                    if applied
                    else "会话代理未在听，不能把未注入写成断网成功。"
                ),
            }

    def network_fault_status(self) -> dict[str, Any]:
        with self._lock:
            alive = self._server is not None and self._server.alive
            mode = self._fault_mode if alive else "none"
            applied = bool(alive and mode in {"offline", "timeout"})
            return {
                "supported": applied,
                "result": (
                    "network_fault_injected"
                    if applied
                    else "network_fault_unsupported_on_app"
                ),
                "evidence_status": "captured" if applied else "evidence_insufficient",
                "fault_mode": mode,
                "scope": "http_proxy_traffic_only",
            }

    def peek(self, session_id: str) -> list[dict[str, Any]]:
        """复制会话代理记录，不清空。"""

        with self._lock:
            buf = self._sessions.get(str(session_id or "").strip())
            if buf is None:
                return []
            return [dict(item) for item in buf.entries]

    def clear_weak_network_shape(self) -> None:
        with self._lock:
            self._shape_latency_ms = 0
            if self._server is not None:
                self._server.set_shape_latency_ms(0)

    def weak_network_shape_status(self) -> dict[str, Any]:
        with self._lock:
            latency = int(self._shape_latency_ms)
            applied = bool(
                self._server is not None and self._server.alive and latency > 0
            )
            return {
                "enabled": applied,
                "latency_ms": latency,
                "throttle_kind": "session_http_proxy" if applied else None,
                "network_throttle_applied": applied,
                "scope": "http_proxy_traffic_only",
            }

    def record_ca_install_result(
        self,
        session_id: str | None,
        result: Mapping[str, Any],
    ) -> None:
        """记录 MITM CA 安装审计结果（按会话可查询）。"""
        payload = dict(result)
        payload["recorded_at"] = datetime.now(UTC).isoformat()
        with self._lock:
            self._last_ca_install = payload
            sid = str(session_id or "").strip()
            if sid:
                self._ca_install_by_session[sid] = payload

    def ca_install_status(self, session_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            sid = str(session_id or "").strip()
            if sid and sid in self._ca_install_by_session:
                return dict(self._ca_install_by_session[sid])
            return dict(self._last_ca_install) if self._last_ca_install else None

    def ensure_proxy_started(self, settings: AppNetworkProxySettings | None = None) -> bool:
        """启动本地录音代理；失败时返回 False（调用方可降级 dumpsys）。"""
        cfg = settings or self._settings
        if cfg is None or not cfg.enabled:
            return False
        with self._lock:
            self._settings = cfg
            if self._server is not None and self._server.alive:
                return True
            server = _RecordingProxyServer(
                host=cfg.listen_host,
                port=cfg.listen_port,
                on_entry=self._fanout_entry,
                mitm_enabled=cfg.mitm_enabled,
                mitm_ca_dir=cfg.mitm_ca_dir,
                shape_latency_ms=self._shape_latency_ms or cfg.shape_latency_ms,
            )
            try:
                server.start()
            except OSError as exc:
                logger.warning("App 网络代理启动失败: %s", exc)
                self._server = None
                return False
            self._server = server
            logger.info(
                "App 网络代理已监听 %s:%s advertise=%s mitm=%s",
                cfg.listen_host,
                cfg.listen_port,
                cfg.endpoint_ref(),
                bool(cfg.mitm_enabled and server.mitm_ready),
            )
            return True

    def mitm_ca_cert_pem(self) -> bytes | None:
        """返回当前进程 MITM CA PEM（未启用则 None）。"""
        with self._lock:
            if self._server is None:
                return None
            return self._server.mitm_ca_cert_pem()

    def bind_session(self, session_id: str, *, endpoint_ref: str | None = None) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("session_id 不能为空")
        with self._lock:
            buf = self._sessions.get(sid)
            if buf is None:
                buf = _SessionBuffer()
                self._sessions[sid] = buf
            if endpoint_ref:
                buf.proxy_endpoint = endpoint_ref
            elif self._settings is not None:
                buf.proxy_endpoint = self._settings.endpoint_ref()

    def unbind_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(str(session_id or "").strip(), None)

    def record_entry(self, session_id: str, entry: Mapping[str, Any]) -> None:
        """测试 / 旁路注入单会话条目。"""
        sid = str(session_id or "").strip()
        if not sid:
            return
        with self._lock:
            buf = self._sessions.setdefault(sid, _SessionBuffer())
            buf.entries.append(_normalize_entry(entry))

    def drain(self, session_id: str) -> list[dict[str, Any]]:
        sid = str(session_id or "").strip()
        with self._lock:
            buf = self._sessions.get(sid)
            if buf is None:
                return []
            items = list(buf.entries)
            buf.entries.clear()
            return items

    def is_session_bound(self, session_id: str) -> bool:
        with self._lock:
            return str(session_id or "").strip() in self._sessions

    def build_network_trace(
        self,
        *,
        session_id: str,
        package_name: str | None = None,
        fallback_summary: str | None = None,
    ) -> dict[str, Any]:
        """优先输出会话代理证据；无绑定时回退 dumpsys 摘要。"""
        bound = self.is_session_bound(session_id)
        entries = self.drain(session_id) if bound else []
        endpoint = None
        with self._lock:
            buf = self._sessions.get(str(session_id or "").strip())
            if buf is not None:
                endpoint = buf.proxy_endpoint
            elif self._settings is not None and self._settings.enabled:
                endpoint = self._settings.endpoint_ref()
        if bound or (self._settings is not None and self._settings.enabled):
            status = "captured" if entries else "empty"
            payload = bound_network_trace_payload(
                entries=entries,
                package_name=package_name,
                proxy_attached=True,
                status=status,
                reason="session_proxy" if entries else "session_proxy_empty",
            )
            payload["proxy_endpoint_ref"] = endpoint
            return payload
        return bound_network_trace_payload(
            package_name=package_name,
            proxy_attached=False,
            raw_summary=fallback_summary or "",
            status="captured" if fallback_summary else "partial_unavailable",
            reason="dumpsys_network_summary",
        )

    def _fanout_entry(self, entry: Mapping[str, Any]) -> None:
        normalized = _normalize_entry(entry)
        with self._lock:
            for buf in self._sessions.values():
                buf.entries.append(dict(normalized))


_HUB = AppNetworkProxyHub()


def get_app_network_proxy_hub() -> AppNetworkProxyHub:
    return _HUB


def reset_app_network_proxy_hub_for_tests() -> None:
    """单测重置全局代理中枢。"""
    global _HUB
    with _HUB._lock:  # noqa: SLF001
        if _HUB._server is not None:  # noqa: SLF001
            try:
                _HUB._server.stop()  # noqa: SLF001
            except Exception:  # noqa: BLE001
                pass
    _HUB = AppNetworkProxyHub()


def attach_device_http_proxy(ops: Any, *, endpoint_ref: str) -> None:
    """经 ADB 设置设备全局 HTTP 代理。"""
    host_port = str(endpoint_ref or "").strip()
    if not host_port or ":" not in host_port or " " in host_port:
        raise ValueError("proxy endpoint 非法")
    ops._shell_fn(["settings", "put", "global", "http_proxy", host_port], 20.0)  # noqa: SLF001


def clear_device_http_proxy(ops: Any) -> None:
    """清除设备全局 HTTP 代理。"""
    try:
        ops._shell_fn(["settings", "put", "global", "http_proxy", ":0"], 20.0)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        logger.info("清除设备 HTTP 代理跳过: %s", exc)


def install_device_mitm_ca(ops: Any, *, ca_cert_pem: bytes) -> dict[str, Any]:
    """尽力把 MITM CA 装到设备信任链，并返回可审计状态。

    优先级：系统 cacerts（可调试）→ 用户 cacerts-added（需 root）→ stage + 拉起安装 Intent。
    status: installed_system | installed_user | staged_user_prompted | staged_user | failed。
    """
    from services.api.app.application.app_network_mitm_certs import android_system_ca_filename

    pem = bytes(ca_cert_pem or b"")
    if b"BEGIN CERTIFICATE" not in pem:
        raise ValueError("ca_cert_pem 非法")
    pem_text = pem.decode("ascii", errors="ignore")
    result: dict[str, Any] = {
        "status": "failed",
        "staged_path": None,
        "system_path": None,
        "user_path": None,
        "verified": False,
        "installer_launched": False,
        "reason": None,
    }
    staged = "/sdcard/Download/alioth-mitm-ca.crt"
    try:
        _adb_push_text(ops, remote_path=staged, content=pem_text)
        result["staged_path"] = staged
        result["status"] = "staged_user"
    except Exception as exc:  # noqa: BLE001
        result["reason"] = f"stage_failed:{type(exc).__name__}"
        return result

    filename = android_system_ca_filename(pem)
    try:
        debuggable = str(
            ops.shell_output(["getprop", "ro.debuggable"], timeout=10.0) or ""
        ).strip()
    except Exception:  # noqa: BLE001
        debuggable = "0"

    if debuggable == "1":
        try:
            system_path = f"/system/etc/security/cacerts/{filename}"
            tmp_path = f"/data/local/tmp/{filename}"
            _adb_push_text(ops, remote_path=tmp_path, content=pem_text)
            try:
                ops._shell_fn(["su", "0", "mount", "-o", "rw,remount", "/system"], 20.0)  # noqa: SLF001
            except Exception:  # noqa: BLE001
                try:
                    ops._shell_fn(["mount", "-o", "rw,remount", "/system"], 20.0)  # noqa: SLF001
                except Exception as remount_exc:  # noqa: BLE001
                    result["reason"] = f"remount_failed:{type(remount_exc).__name__}"
                    _launch_ca_installer(ops, staged_path=staged, result=result)
                    return result
            copied = False
            for copy_args in (
                ["su", "0", "cp", tmp_path, system_path],
                ["cp", tmp_path, system_path],
            ):
                try:
                    ops._shell_fn(copy_args, 20.0)  # noqa: SLF001
                    copied = True
                    break
                except Exception:  # noqa: BLE001
                    continue
            if not copied:
                result["reason"] = "system_copy_failed"
                _launch_ca_installer(ops, staged_path=staged, result=result)
                return result
            try:
                ops._shell_fn(["su", "0", "chmod", "644", system_path], 15.0)  # noqa: SLF001
            except Exception:  # noqa: BLE001
                ops._shell_fn(["chmod", "644", system_path], 15.0)  # noqa: SLF001
            result["system_path"] = system_path
            verified = _verify_remote_file(ops, system_path)
            result["verified"] = verified
            if verified:
                result["status"] = "installed_system"
                result["reason"] = "system_cacerts"
                return result
            result["reason"] = "system_copy_unverified"
        except Exception as exc:  # noqa: BLE001
            result["reason"] = f"system_install_failed:{type(exc).__name__}"

    # 用户信任存储（需 root；非 debug 也可试）
    user_path = f"/data/misc/user/0/cacerts-added/{filename}"
    try:
        tmp_path = f"/data/local/tmp/{filename}"
        try:
            _adb_push_text(ops, remote_path=tmp_path, content=pem_text)
        except Exception:  # noqa: BLE001
            tmp_path = staged
        for copy_args in (
            ["su", "0", "mkdir", "-p", "/data/misc/user/0/cacerts-added"],
            ["su", "0", "cp", tmp_path, user_path],
            ["su", "0", "chmod", "644", user_path],
        ):
            ops._shell_fn(copy_args, 20.0)  # noqa: SLF001
        if _verify_remote_file(ops, user_path):
            result["user_path"] = user_path
            result["verified"] = True
            result["status"] = "installed_user"
            result["reason"] = "user_cacerts_added"
            return result
    except Exception as exc:  # noqa: BLE001
        if not result.get("reason"):
            result["reason"] = f"user_install_failed:{type(exc).__name__}"

    if not result.get("reason"):
        result["reason"] = (
            "device_not_debuggable_staged_only"
            if debuggable != "1"
            else "trust_install_incomplete"
        )
    _launch_ca_installer(ops, staged_path=staged, result=result)
    return result


def _verify_remote_file(ops: Any, remote_path: str) -> bool:
    remote = str(remote_path or "").strip()
    if not remote.startswith("/"):
        return False
    try:
        out = str(
            ops.shell_output(["ls", "-l", remote], timeout=10.0) or ""
        ).strip()
        return bool(out) and "No such file" not in out and remote.split("/")[-1] in out
    except Exception:  # noqa: BLE001
        return False


def _launch_ca_installer(
    ops: Any,
    *,
    staged_path: str,
    result: dict[str, Any],
) -> None:
    """拉起系统证书安装器；用户仍须确认信任（可审计）。"""
    try:
        ops._shell_fn(  # noqa: SLF001
            [
                "am",
                "start",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                f"file://{staged_path}",
                "-t",
                "application/x-x509-ca-cert",
            ],
            20.0,
        )
        result["installer_launched"] = True
        if result.get("status") in {"staged_user", "failed"}:
            result["status"] = "staged_user_prompted"
        if not result.get("reason") or str(result.get("reason")).startswith("device_not"):
            result["reason"] = "installer_intent_launched"
    except Exception as exc:  # noqa: BLE001
        result["installer_launched"] = False
        if not result.get("reason"):
            result["reason"] = f"installer_launch_failed:{type(exc).__name__}"


def _adb_push_text(ops: Any, *, remote_path: str, content: str) -> None:
    """经 shell 重定向写入小文本文件（避免依赖 adb push 二进制通道）。"""
    import base64

    remote = str(remote_path or "").strip()
    if not remote.startswith("/") or " " in remote or ";" in remote:
        raise ValueError("remote_path 非法")
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    chunk_size = 2000
    ops._shell_fn(["sh", "-c", f"rm -f {remote} {remote}.b64"], 10.0)  # noqa: SLF001
    for index in range(0, len(encoded), chunk_size):
        part = encoded[index : index + chunk_size]
        op = ">>" if index else ">"
        ops._shell_fn(  # noqa: SLF001
            ["sh", "-c", f"printf '%s' '{part}' {op} {remote}.b64"],
            15.0,
        )
    ops._shell_fn(  # noqa: SLF001
        ["sh", "-c", f"base64 -d {remote}.b64 > {remote} && rm -f {remote}.b64"],
        20.0,
    )


def _normalize_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    method = str(entry.get("method") or "GET").upper()[:16]
    host = str(entry.get("url_host") or entry.get("host") or "")[:200]
    path = str(entry.get("path") or "/")[:200]
    status = entry.get("status_code")
    if isinstance(status, bool) or (status is not None and not isinstance(status, int)):
        status = None
    payload = {
        "method": method,
        "url_host": host or None,
        "path": path or None,
        "status_code": status,
        "bytes": entry.get("bytes") if isinstance(entry.get("bytes"), int) else None,
        "observed_at": str(entry.get("observed_at") or datetime.now(UTC).isoformat()),
    }
    if entry.get("tls_mitm") is True:
        payload["tls_mitm"] = True
    if entry.get("tls_mode"):
        payload["tls_mode"] = str(entry.get("tls_mode"))[:40]
    if entry.get("mitm_reason"):
        payload["mitm_reason"] = str(entry.get("mitm_reason"))[:120]
    return payload


class _RecordingProxyServer:
    """HTTP/HTTPS 录音代理；可选 TLS MITM 解密请求行（不存 body）。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        on_entry: Any,
        mitm_enabled: bool = False,
        mitm_ca_dir: str | None = None,
        shape_latency_ms: int = 0,
    ) -> None:
        self._host = host
        self._port = port
        self._on_entry = on_entry
        self._mitm_enabled = bool(mitm_enabled)
        self._mitm_ca_dir = mitm_ca_dir
        self._mitm_authority: Any | None = None
        self._shape_latency_ms = max(0, min(int(shape_latency_ms or 0), 10_000))
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.alive = False
        self.mitm_ready = False
        if self._mitm_enabled:
            self._try_init_mitm()

    def set_shape_latency_ms(self, latency_ms: int) -> None:
        self._shape_latency_ms = max(0, min(int(latency_ms or 0), 10_000))

    def set_fault_mode(self, mode: str) -> None:
        requested = str(mode or "none").strip().casefold()
        self._fault_mode = requested if requested in {"offline", "timeout", "none"} else "none"

    def _apply_shape_delay(self) -> None:
        latency = int(self._shape_latency_ms)
        if latency <= 0:
            return
        import time

        time.sleep(latency / 1000.0)

    def mitm_ca_cert_pem(self) -> bytes | None:
        if self._mitm_authority is None:
            return None
        return self._mitm_authority.ca_cert_pem

    def _try_init_mitm(self) -> None:
        try:
            from pathlib import Path

            from services.api.app.application.app_network_mitm_certs import (
                AppNetworkMitmAuthority,
                cryptography_available,
            )

            if not cryptography_available():
                logger.warning("TLS MITM 已请求但 cryptography 不可用，降级 CONNECT-only")
                self._mitm_enabled = False
                self.mitm_ready = False
                return
            storage = Path(self._mitm_ca_dir) if self._mitm_ca_dir else (
                Path(".alioth") / "app-network-mitm"
            )
            self._mitm_authority = AppNetworkMitmAuthority(storage_dir=storage)
            self.mitm_ready = True
            logger.info("App 网络代理 TLS MITM 已启用 CA=%s", storage)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TLS MITM 初始化失败，降级 CONNECT-only: %s", exc)
            self._mitm_enabled = False
            self._mitm_authority = None
            self.mitm_ready = False

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._host, self._port))
        sock.listen(64)
        sock.settimeout(1.0)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve,
            name="alioth-app-net-proxy",
            daemon=True,
        )
        self._thread.start()
        self.alive = True

    def stop(self) -> None:
        self._stop.set()
        self.alive = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                client, _addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_client,
                args=(client,),
                daemon=True,
            ).start()

    def _handle_client(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            fault = getattr(self, "_fault_mode", "none")
            if fault == "offline":
                return
            if fault == "timeout":
                import time

                time.sleep(8.0)
                return
            client.settimeout(10.0)
            data = client.recv(65535)
            if not data:
                return
            self._apply_shape_delay()
            header_text = data.decode("iso-8859-1", errors="replace")
            first = header_text.split("\r\n", 1)[0]
            parts = first.split(" ")
            method = parts[0].upper() if parts else "GET"
            target = parts[1] if len(parts) > 1 else "/"
            if method == "CONNECT":
                host, _, port_text = target.partition(":")
                port = int(port_text or "443")
                if self._mitm_enabled and self._mitm_authority is not None:
                    # MITM 路径会先回 200；失败后不可再走明文 CONNECT 隧道
                    self._handle_connect_mitm(client=client, host=host, port=port)
                    return
                upstream = socket.create_connection((host, port), timeout=10.0)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._on_entry(
                    {
                        "method": "CONNECT",
                        "url_host": host,
                        "path": f":{port}",
                        "status_code": 200,
                        "tls_mode": "connect_tunnel",
                        "mitm_reason": "connect_only",
                    }
                )
                _tunnel(client, upstream)
                return
            host = _host_from_http(header_text, target)
            path = target
            if target.startswith("http://"):
                split = urlsplit(target)
                host = split.hostname or host
                path = split.path or "/"
                if split.query:
                    path = f"{path}?{split.query}"
            port = 80
            upstream = socket.create_connection((host, port), timeout=10.0)
            upstream.sendall(data)
            response = upstream.recv(65535)
            status_code = _status_from_response(response)
            self._on_entry(
                {
                    "method": method,
                    "url_host": host,
                    "path": path,
                    "status_code": status_code,
                    "bytes": len(response) if response else 0,
                    "tls_mode": "plaintext_http",
                }
            )
            if response:
                client.sendall(response)
            _tunnel(client, upstream)
        except Exception as exc:  # noqa: BLE001
            logger.debug("录音代理处理失败: %s", exc)
        finally:
            try:
                client.close()
            except OSError:
                pass
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass

    def _handle_connect_mitm(
        self,
        *,
        client: socket.socket,
        host: str,
        port: int,
    ) -> None:
        """尝试 MITM：仅记录解密后的请求行与状态码，永不存储 body。"""
        from services.api.app.application.app_network_mitm_certs import (
            build_upstream_ssl_context,
        )

        assert self._mitm_authority is not None
        client_ssl = None
        upstream_ssl = None
        try:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            server_ctx = self._mitm_authority.ssl_context_for_host(host)
            client_ssl = server_ctx.wrap_socket(client, server_side=True)
            client_ssl.settimeout(10.0)
            upstream = socket.create_connection((host, port), timeout=10.0)
            upstream_ctx = build_upstream_ssl_context()
            upstream_ssl = upstream_ctx.wrap_socket(upstream, server_hostname=host)
            upstream_ssl.settimeout(10.0)

            req = client_ssl.recv(65535)
            if not req:
                return
            req_text = req.decode("iso-8859-1", errors="replace")
            first = req_text.split("\r\n", 1)[0]
            parts = first.split(" ")
            method = parts[0].upper() if parts else "GET"
            path = parts[1] if len(parts) > 1 else "/"
            if path.startswith("http://") or path.startswith("https://"):
                split = urlsplit(path)
                path = split.path or "/"
            # 证据只保留 path，丢弃 query（避免 token）
            path = path.split("?", 1)[0][:200]
            upstream_ssl.sendall(req)
            response = upstream_ssl.recv(65535)
            status_code = _status_from_response(response)
            self._on_entry(
                {
                    "method": method,
                    "url_host": host,
                    "path": path,
                    "status_code": status_code,
                    "bytes": len(response) if response else 0,
                    "tls_mitm": True,
                    "tls_mode": "mitm_plaintext",
                }
            )
            if response:
                client_ssl.sendall(response)
            _tunnel(client_ssl, upstream_ssl)
        except Exception as exc:  # noqa: BLE001
            logger.debug("MITM 处理失败: %s", exc)
            self._on_entry(
                {
                    "method": "CONNECT",
                    "url_host": host,
                    "path": f":{port}",
                    "status_code": None,
                    "tls_mode": "mitm_failed",
                    "mitm_reason": f"{type(exc).__name__}",
                }
            )
        finally:
            for sock in (client_ssl, upstream_ssl):
                if sock is None:
                    continue
                try:
                    sock.close()
                except OSError:
                    pass


def _tunnel(a: socket.socket, b: socket.socket) -> None:
    stop = threading.Event()

    def pipe(src: socket.socket, dst: socket.socket) -> None:
        try:
            while not stop.is_set():
                chunk = src.recv(65535)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            stop.set()
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t1 = threading.Thread(target=pipe, args=(a, b), daemon=True)
    t2 = threading.Thread(target=pipe, args=(b, a), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=30.0)
    t2.join(timeout=30.0)


def _host_from_http(header_text: str, target: str) -> str:
    for line in header_text.split("\r\n"):
        if line.lower().startswith("host:"):
            return line.split(":", 1)[1].strip().split(":")[0]
    if target.startswith("http://"):
        return urlsplit(target).hostname or "invalid.local"
    return "invalid.local"


def _status_from_response(response: bytes) -> int | None:
    try:
        line = response.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
        parts = line.split(" ")
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1])
    except Exception:  # noqa: BLE001
        return None
    return None
