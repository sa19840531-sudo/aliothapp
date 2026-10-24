"""锁版 Playwright CLI 的有界子进程；不接受 Shell 或自由命令。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

CLI_VERSION = "0.1.19"
CLI_PATCH_ID = "alioth-cdp-context-inheritance-v1"
CLI_BUNDLE_SHA256 = "c59cd8cd2f8b26153b9691de09d9b11885c675d08ab83e6242cfee54d380f2b6"
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_READ_COMMANDS = frozenset({"snapshot", "screenshot", "requests", "tab-list"})
_ACTION_COMMANDS = frozenset({"goto", "click", "go-back", "mousemove", "mousewheel"})
_FORBIDDEN_TARGET = re.compile(r"^(?:-|javascript:|file:|data:|getBy|locator\s*\()", re.I)
_BACKEND_NODE_TARGET = re.compile(
    r"^backend-node-id=(\d+);selector=([A-Za-z0-9_-]+);"
    r"semantic-sha256=([a-f0-9]{64})$"
)
_MAX_BACKEND_NODE_ID = 2**31 - 1
_MAX_BACKEND_SELECTOR_LENGTH = 3000
_SEMANTIC_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class PlaywrightCliError(RuntimeError):
    """错误只返回固定类别，避免命令输出里的页面内容或凭证进入日志。"""

    def __init__(self, code: str, *, action_uncertain: bool = False) -> None:
        super().__init__(code)
        self.error_code = code
        self.action_uncertain = action_uncertain
        self.retryable = False


@dataclass(frozen=True)
class CliReceipt:
    command: str
    version: str
    argument_hash: str
    elapsed_ms: int
    output: dict

    def audit(self) -> dict:
        """对外仅发布命令类别及摘要，原始输出作为受控证据处理。"""
        return {
            "provider": "playwright_cli",
            "version": self.version,
            "compatibility_patch": CLI_PATCH_ID,
            "command": self.command,
            "argument_hash": self.argument_hash,
            "elapsed_ms": self.elapsed_ms,
            "command_succeeded": True,
            "arrival_verified": False,
        }


class PlaywrightCliProcess:
    """每个浏览器会话独占工作目录和 CLI 名称；只连接平台创建的本机浏览器。"""

    def __init__(
        self,
        *,
        script: Path,
        workspace: Path,
        session_id: str,
        actions_enabled: bool,
        timeout_seconds: float = 20,
    ) -> None:
        if not session_id or not 0 < timeout_seconds <= 60:
            raise ValueError("CLI 会话和超时配置无效")
        self.script = script.resolve(strict=True)
        self.workspace = workspace.resolve(strict=True)
        self.session_name = "alioth-" + hashlib.sha256(session_id.encode()).hexdigest()[:24]
        self.actions_enabled = actions_enabled
        self.timeout_seconds = timeout_seconds
        self._attached = False
        self._poisoned = False
        self._quarantine_reason: str | None = None
        self._verified = False
        self._context_options: dict | None = None
        self.daemon_pid: int | None = None
        self._lock = asyncio.Lock()
        self._node = shutil.which("node")
        if self._node is None:
            raise PlaywrightCliError("CLI_NODE_UNAVAILABLE")
        # CLI 以 .playwright 所在目录计算会话命名空间，防止同机任务共享状态。
        (self.workspace / ".playwright").mkdir(mode=0o700, exist_ok=True)

    def configure_environment(self, *, viewport: dict, context_options: dict) -> None:
        """启动时继承 Python 浏览器的固定设备参数，禁止会话中重新配置。"""
        if self._attached or self._context_options is not None:
            raise PlaywrightCliError("CLI_ENVIRONMENT_ALREADY_BOUND")
        screen = context_options.get("screen", viewport)
        for size in (viewport, screen):
            if (
                not isinstance(size, dict)
                or set(size) != {"width", "height"}
                or any(
                    type(size[key]) is not int or not 1 <= size[key] <= 10000
                    for key in ("width", "height")
                )
            ):
                raise PlaywrightCliError("CLI_ENVIRONMENT_REJECTED")
        dpr = context_options.get("device_scale_factor", 1)
        mobile = context_options.get("is_mobile", False)
        touch = context_options.get("has_touch", False)
        if (
            type(dpr) not in {int, float}
            or not 0.5 <= dpr <= 4
            or type(mobile) is not bool
            or type(touch) is not bool
        ):
            raise PlaywrightCliError("CLI_ENVIRONMENT_REJECTED")
        self._context_options = {
            "viewport": dict(viewport),
            "screen": dict(screen),
            "deviceScaleFactor": dpr,
            "isMobile": mobile,
            "hasTouch": touch,
        }

    async def attach(self, endpoint: str) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or not parsed.port
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise PlaywrightCliError("CLI_ENDPOINT_REJECTED")
        async with self._lock:
            if self._attached:
                raise PlaywrightCliError("CLI_ALREADY_ATTACHED")
            package = json.loads((self.script.parent / "package.json").read_text(encoding="utf-8"))
            if package.get("name") != "@playwright/cli" or package.get("version") != CLI_VERSION:
                raise PlaywrightCliError("CLI_VERSION_MISMATCH")
            # 仅允许经过锁版审核的完整依赖文件，缺补丁或被改写都不能连接任务浏览器。
            bundle = self.script.parents[2] / "playwright-core" / "lib" / "coreBundle.js"
            try:
                bundle_sha256 = hashlib.sha256(bundle.read_bytes()).hexdigest()
            except OSError as exc:
                raise PlaywrightCliError("CLI_BUNDLE_UNVERIFIED") from exc
            if bundle_sha256 != CLI_BUNDLE_SHA256:
                raise PlaywrightCliError("CLI_BUNDLE_UNVERIFIED")
            if self._context_options is None:
                raise PlaywrightCliError("CLI_ENVIRONMENT_UNBOUND")
            version = await self._invoke(("--version",), action=False)
            if version.get("version") != CLI_VERSION:
                raise PlaywrightCliError("CLI_VERSION_MISMATCH")
            self._verified = True
            # attach 失败也要尝试 detach，处理已启动 daemon 而响应丢失的情况。
            self._attached = True
            try:
                attached = await self._invoke(("attach", "--cdp=" + endpoint), action=False)
                pid = attached.get("pid")
                if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
                    raise PlaywrightCliError("CLI_DAEMON_IDENTITY_MISSING")
                self.daemon_pid = pid
            except BaseException:
                self._poisoned = True
                raise

    async def run(self, command: str, *arguments: str, _internal: bool = False) -> CliReceipt:
        async with self._lock:
            if not self._attached or not self._verified or self._poisoned:
                raise PlaywrightCliError("CLI_SESSION_UNAVAILABLE")
            self._validate(command, arguments, allow_backend_node=_internal)
            return await self._run_validated(command, arguments, action=command in _ACTION_COMMANDS)

    async def run_readonly(self, command: str, *arguments: str) -> CliReceipt:
        """在隔离后的同一浏览器中只读取证，不恢复或旁路任何动作能力。"""
        async with self._lock:
            if not self._attached or not self._verified:
                raise PlaywrightCliError("CLI_SESSION_UNAVAILABLE")
            if command not in _READ_COMMANDS:
                raise PlaywrightCliError("CLI_READONLY_DIAGNOSTIC_ONLY")
            self._validate(command, arguments)
            return await self._run_validated(command, arguments, action=False)

    async def click_backend_node(
        self,
        backend_node_id: int,
        selector: str,
        semantic_digest: str | None = None,
    ) -> CliReceipt:
        """点击预检绑定的 Chromium 节点，并在 CLI 侧复核节点与语义摘要。"""
        if (
            type(backend_node_id) is not int
            or not 0 < backend_node_id <= _MAX_BACKEND_NODE_ID
        ):
            raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
        if (
            not isinstance(selector, str)
            or not selector
            or len(selector) > _MAX_BACKEND_SELECTOR_LENGTH
            or "\x00" in selector
        ):
            raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
        if not isinstance(semantic_digest, str) or not _SEMANTIC_DIGEST.fullmatch(
            semantic_digest
        ):
            raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
        encoded_selector = base64.urlsafe_b64encode(selector.encode("utf-8")).decode("ascii")
        encoded_selector = encoded_selector.rstrip("=")
        return await self.run(
            "click",
            f"backend-node-id={backend_node_id};selector={encoded_selector};"
            f"semantic-sha256={semantic_digest}",
            _internal=True,
        )

    def require_action_session(self) -> None:
        """未知动作结果后也禁止从同一页面旁路改用旧执行器。"""
        if self._poisoned or not self._attached or not self._verified:
            raise PlaywrightCliError("CLI_SESSION_UNAVAILABLE")

    @property
    def is_quarantined(self) -> bool:
        """是否已隔离；隔离只禁止动作，不妨碍后续只读取证。"""
        return self._poisoned

    @property
    def quarantine_reason(self) -> str | None:
        """隔离原因，供只读诊断 Capture 作为审计元数据留存。"""
        return self._quarantine_reason

    def quarantine(self, *, reason: str = "environment_changed") -> None:
        """隔离会话；后续只能读取证，不能切换旧执行器继续动作。"""
        self._poisoned = True
        self._quarantine_reason = reason

    async def _run_validated(
        self,
        command: str,
        arguments: tuple[str, ...],
        *,
        action: bool,
    ) -> CliReceipt:
        started = time.monotonic()
        try:
            output = await self._invoke((command, *arguments), action=action)
        except BaseException:
            if action:
                self.quarantine(reason="action_outcome_uncertain")
            raise
        return CliReceipt(
            command=command,
            version=CLI_VERSION,
            argument_hash=hashlib.sha256(
                json.dumps([command, *arguments], ensure_ascii=False).encode()
            ).hexdigest(),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            output=output,
        )

    async def detach(self) -> None:
        async with self._lock:
            if not self._attached:
                return
            try:
                await self._invoke(("detach",), action=False)
            finally:
                self._attached = False
                self._poisoned = True

    def _validate(
        self,
        command: str,
        arguments: tuple[str, ...],
        *,
        allow_backend_node: bool = False,
    ) -> None:
        if command not in _READ_COMMANDS | _ACTION_COMMANDS:
            raise PlaywrightCliError("CLI_COMMAND_REJECTED")
        if command in _ACTION_COMMANDS and not self.actions_enabled:
            raise PlaywrightCliError("CLI_READ_ONLY")
        if any(not isinstance(v, str) or len(v) > 4096 or "\x00" in v for v in arguments):
            raise PlaywrightCliError("CLI_ARGUMENT_REJECTED")
        if command in {"snapshot", "requests", "tab-list", "go-back"} and arguments:
            raise PlaywrightCliError("CLI_ARGUMENT_REJECTED")
        if command == "screenshot":
            if (
                len(arguments) != 2
                or not arguments[0].startswith("--filename=")
                or arguments[1] != "--hires"
            ):
                raise PlaywrightCliError("CLI_ARTIFACT_PATH_REJECTED")
            target = Path(arguments[0].split("=", 1)[1])
            if (
                not target.is_absolute()
                or target.is_symlink()
                or target.resolve().parent != self.workspace
                or not re.fullmatch(r"capture-[a-f0-9]{32}\.png", target.name)
            ):
                raise PlaywrightCliError("CLI_ARTIFACT_PATH_REJECTED")
        if command == "goto":
            if len(arguments) != 1:
                raise PlaywrightCliError("CLI_ARGUMENT_REJECTED")
            target = urlsplit(arguments[0])
            if (
                target.scheme not in {"http", "https"}
                or not target.hostname
                or target.username
                or target.password
                or any(c.isspace() for c in arguments[0])
            ):
                raise PlaywrightCliError("CLI_URL_REJECTED")
        if command == "click":
            if len(arguments) != 1 or not arguments[0] or _FORBIDDEN_TARGET.match(arguments[0]):
                raise PlaywrightCliError("CLI_TARGET_REJECTED")
            if arguments[0].startswith("backend-node-id="):
                backend_target = _BACKEND_NODE_TARGET.fullmatch(arguments[0])
                if backend_target is None or not allow_backend_node:
                    raise PlaywrightCliError("CLI_TARGET_REJECTED")
                try:
                    backend_node_id = int(backend_target[1])
                except ValueError as exc:
                    raise PlaywrightCliError("CLI_TARGET_REJECTED") from exc
                if not 0 < backend_node_id <= _MAX_BACKEND_NODE_ID:
                    raise PlaywrightCliError("CLI_TARGET_REJECTED")
        if command in {"mousemove", "mousewheel"}:
            if len(arguments) != 2 or any(not re.fullmatch(r"-?\d{1,5}", v) for v in arguments):
                raise PlaywrightCliError("CLI_COORDINATE_REJECTED")
            if any(abs(int(v)) > 10000 for v in arguments):
                raise PlaywrightCliError("CLI_COORDINATE_REJECTED")
            if command == "mousemove" and any(int(v) < 0 for v in arguments):
                raise PlaywrightCliError("CLI_COORDINATE_REJECTED")

    async def _invoke(self, args: tuple[str, ...], *, action: bool) -> dict:
        # 仅保留运行所需环境，不将平台数据库、模型凭证或代理秘密传给 CLI。
        env = {
            k: os.environ[k] for k in ("PATH", "SystemRoot", "TMPDIR", "LANG") if k in os.environ
        }
        env.update({"CI": "1", "NO_UPDATE_NOTIFIER": "1"})
        # 该锁版 CLI 的注册目录开关也隔离 socket、错误日志和后台会话记录。
        env["PWTEST_DAEMON_SESSION_DIR"] = str(self.workspace / "daemon")
        # 不继承用户全局 CLI 配置（其中可包含脚本、代理和其他浏览器句柄）。
        env["PWTEST_CLI_GLOBAL_CONFIG"] = str(self.workspace / "global")
        env["PWTEST_SERVER_REGISTRY"] = str(self.workspace / "browser-registry")
        # 非 root、只读镜像也只能在任务目录写缓存，不访问用户的共享缓存。
        env["XDG_CACHE_HOME"] = str(self.workspace / "cache")
        if self._context_options is not None:
            env["ALIOTH_CLI_CONTEXT_OPTIONS"] = json.dumps(self._context_options)
        process = await asyncio.create_subprocess_exec(
            self._node,
            str(self.script),
            "-s=" + self.session_name,
            "--json",
            *args,
            cwd=self.workspace,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        readers = [
            asyncio.create_task(_read_bounded(process.stdout)),
            asyncio.create_task(_read_bounded(process.stderr)),
        ]
        try:
            async with asyncio.timeout(self.timeout_seconds):
                stdout, _stderr = await asyncio.gather(*readers)
                await process.wait()
            if process.returncode != 0:
                raise PlaywrightCliError("CLI_COMMAND_FAILED", action_uncertain=action)
            try:
                payload = json.loads(stdout)
            except (ValueError, UnicodeDecodeError) as exc:
                raise PlaywrightCliError("CLI_INVALID_RESPONSE", action_uncertain=action) from exc
            if not isinstance(payload, dict) or payload.get("isError") or payload.get("error"):
                raise PlaywrightCliError("CLI_COMMAND_FAILED", action_uncertain=action)
            return payload
        except BaseException as exc:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            if process.returncode is None:
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
            # 超限读取中断后必须排空已关闭子进程的管道，否则 StreamReader 暂停会
            # 让 wait 永久等待。先终止子进程，剩余输出不会继续增长。
            await process.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, PlaywrightCliError):
                raise PlaywrightCliError(
                    exc.error_code,
                    action_uncertain=action or exc.action_uncertain,
                ) from exc
            raise PlaywrightCliError("CLI_RESPONSE_UNAVAILABLE", action_uncertain=action) from exc


async def _read_bounded(stream: asyncio.StreamReader) -> bytes:
    chunks = []
    size = 0
    while chunk := await stream.read(65536):
        size += len(chunk)
        if size > MAX_OUTPUT_BYTES:
            raise PlaywrightCliError("CLI_OUTPUT_LIMIT")
        chunks.append(chunk)
    return b"".join(chunks)
