"""CC智控 WebSocket API（ONIRENT 桌面端）。

文件／測試工具預設：``ws://127.0.0.1:33332/``

取得設備列表：
  請求：``{"id":"...","type":"list"}``
  成功：``{"id":"...","state":0,"message":"","data":["host:port", ...]}``

環境變數：
- ``ALIOTH_ONIRENT_WS_URL``／``ALIOTH_CC_WS_URL``：預設 ``ws://127.0.0.1:33332/``
- ``ALIOTH_ONIRENT_WS_TIMEOUT_SECONDS``：預設 8
- Docker 內請改 ``ws://host.docker.internal:33332/``
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import uuid
from typing import Any
from urllib.parse import urlparse

__all__ = [
    "CcZhikongWsError",
    "fetch_cc_device_list",
    "resolve_onirent_ws_url",
]


class CcZhikongWsError(RuntimeError):
    """CC智控 WebSocket 呼叫失敗。"""


def resolve_onirent_ws_url() -> str:
    return (
        os.getenv("ALIOTH_ONIRENT_WS_URL")
        or os.getenv("ALIOTH_CC_WS_URL")
        or "ws://127.0.0.1:33332/"
    ).strip()


def _ws_timeout_seconds() -> float:
    raw = os.getenv("ALIOTH_ONIRENT_WS_TIMEOUT_SECONDS") or "8"
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 8.0


def _parse_ws_url(url: str) -> tuple[str, int, str]:
    parsed = urlparse(url if "://" in url else f"ws://{url}")
    if parsed.scheme not in ("ws", "http"):
        raise CcZhikongWsError(f"不支援的 WS URL scheme：{parsed.scheme!r}")
    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or 33332)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return host, port, path


class _WsSocket:
    """帶接收緩衝的 TCP socket，避免握手吃掉後續 frame。"""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()

    def settimeout(self, timeout: float) -> None:
        self._sock.settimeout(timeout)

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def recv(self, size: int) -> bytes:
        while len(self._buf) < size:
            piece = self._sock.recv(max(size - len(self._buf), 4096))
            if not piece:
                break
            self._buf.extend(piece)
        if not self._buf:
            return b""
        take = bytes(self._buf[:size])
        del self._buf[:size]
        return take

    def close(self) -> None:
        self._sock.close()


def _recv_exact(sock: _WsSocket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        piece = sock.recv(size - len(chunks))
        if not piece:
            raise CcZhikongWsError("WebSocket 連線已關閉")
        chunks.extend(piece)
    return bytes(chunks)


def _handshake(sock: _WsSocket, host: str, port: int, path: str) -> None:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(req.encode("ascii"))
    raw = bytearray()
    while b"\r\n\r\n" not in raw:
        piece = sock.recv(4096)
        if not piece:
            raise CcZhikongWsError("WebSocket 握手失敗（無回應）")
        raw.extend(piece)
        if len(raw) > 65536:
            raise CcZhikongWsError("WebSocket 握手回應過長")
    header, _, rest = bytes(raw).partition(b"\r\n\r\n")
    if rest:
        # 把同包剩餘 frame 塞回緩衝
        sock._buf[0:0] = rest
    status_line = header.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
    if "101" not in status_line:
        raise CcZhikongWsError(f"WebSocket 握手失敗：{status_line}")
    _ = key  # accept 可選；本機橋接常省略


def _mask_payload(payload: bytes) -> bytes:
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return mask + masked


def _send_text(sock: _WsSocket, text: str) -> None:
    payload = text.encode("utf-8")
    header = bytearray([0x81])  # FIN + text
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    sock.sendall(bytes(header) + _mask_payload(payload))


def _recv_text(sock: _WsSocket) -> str:
    while True:
        b1, b2 = _recv_exact(sock, 2)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", _recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
        mask = _recv_exact(sock, 4) if masked else b""
        payload = _recv_exact(sock, length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x1:  # text
            return payload.decode("utf-8", errors="replace")
        if opcode == 0x8:  # close
            raise CcZhikongWsError("WebSocket 對端關閉連線")
        if opcode in (0x9, 0xA):
            continue


def fetch_cc_device_list(
    *,
    ws_url: str | None = None,
    request_id: str | None = None,
    timeout_seconds: float | None = None,
) -> list[str]:
    """呼叫 ``type=list``，回傳 ADB ``host:port`` 列表。"""
    url = (ws_url or resolve_onirent_ws_url()).strip()
    host, port, path = _parse_ws_url(url)
    timeout = (
        float(timeout_seconds)
        if timeout_seconds is not None
        else _ws_timeout_seconds()
    )
    req_id = (request_id or uuid.uuid4().hex[:12]).strip() or "alioth"
    payload = {"id": req_id, "type": "list"}

    raw_sock = socket.create_connection((host, port), timeout=timeout)
    sock = _WsSocket(raw_sock)
    try:
        sock.settimeout(timeout)
        _handshake(sock, host, port, path)
        _send_text(sock, json.dumps(payload, ensure_ascii=False))
        raw = _recv_text(sock)
    finally:
        try:
            sock.close()
        except OSError:
            pass

    try:
        body: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CcZhikongWsError(f"CC智控回應非 JSON：{raw[:200]}") from exc

    state = body.get("state")
    try:
        state_i = int(state)
    except (TypeError, ValueError):
        state_i = -1
    if state_i != 0:
        msg = str(body.get("message") or "")
        raise CcZhikongWsError(f"CC智控 list 失敗 state={state_i} message={msg}")

    data = body.get("data")
    if not isinstance(data, list):
        raise CcZhikongWsError(f"CC智控 list data 非陣列：{type(data).__name__}")
    endpoints: list[str] = []
    seen: set[str] = set()
    for item in data:
        endpoint = str(item or "").strip()
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        endpoints.append(endpoint)
    return endpoints
