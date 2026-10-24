"""星語 Official 會話庫：記憶體 + 可選磁碟索引（扛住 API --reload）。"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class XingyuSessionRecord:
    """單一會話狀態（禁止序列化連線碼／storage_path／device 句柄）。"""

    session_id: str
    task_id: str
    session_handle: str
    backend: str
    created_at: float
    serial: str | None = None
    package_name: str | None = None
    activity: str | None = None
    # 任务备注：同名降版扫描跳过授权须从此保留到安装审计／报告
    task_remark: str | None = None
    # 云手机租约 envId：API reload 后重建装置绑定必需
    morelogin_env_id: str | None = None
    # 進程內裝置句柄；不進 JSON／日誌
    device: Any | None = field(default=None, repr=False)


def _default_persist_path() -> Path | None:
    raw = str(os.getenv("ALIOTH_XINGYU_SESSION_INDEX") or "").strip()
    if raw:
        return Path(raw)
    # 与 Docker 工作区对齐；无 workspace 时退回进程本地
    for candidate in (
        Path("/workspace/.alioth/xingyu-session-index.json"),
        Path(".alioth/xingyu-session-index.json"),
    ):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    return None


class XingyuSessionStore:
    """執行緒安全的 Session 表；磁碟索引只存可重建元数据。"""

    def __init__(self, *, persist_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, XingyuSessionRecord] = {}
        self._by_handle: dict[str, str] = {}
        self._persist_path = (
            persist_path if persist_path is not None else _default_persist_path()
        )

    def create(
        self,
        *,
        session_id: str,
        task_id: str,
        backend: str,
        serial: str | None = None,
        device: Any | None = None,
        task_remark: str | None = None,
        morelogin_env_id: str | None = None,
        session_handle: str | None = None,
    ) -> XingyuSessionRecord:
        handle = (session_handle or "").strip() or f"xyh_{uuid4().hex}"
        remark = str(task_remark).strip() if task_remark is not None else None
        env_id = str(morelogin_env_id).strip() if morelogin_env_id is not None else None
        record = XingyuSessionRecord(
            session_id=session_id,
            task_id=task_id,
            session_handle=handle,
            backend=backend,
            created_at=time.time(),
            serial=serial,
            device=device,
            task_remark=remark or None,
            morelogin_env_id=env_id or None,
        )
        with self._lock:
            existing = self._by_session.get(session_id)
            if existing is not None:
                self._by_handle.pop(existing.session_handle, None)
            old_sid = self._by_handle.get(handle)
            if old_sid is not None and old_sid != session_id:
                old = self._by_session.pop(old_sid, None)
                if old is not None and old.session_handle != handle:
                    self._by_handle.pop(old.session_handle, None)
            self._by_session[session_id] = record
            self._by_handle[handle] = session_id
            self._persist_locked()
        return record

    def get_by_session_id(self, session_id: str) -> XingyuSessionRecord | None:
        with self._lock:
            return self._by_session.get(session_id)

    def get_by_handle(self, session_handle: str) -> XingyuSessionRecord | None:
        with self._lock:
            session_id = self._by_handle.get(session_handle)
            if session_id is None:
                return None
            return self._by_session.get(session_id)

    def lookup_persisted_by_handle(self, session_handle: str) -> dict[str, Any] | None:
        """只读磁盘索引；用于 API reload 后内存空表时恢复。"""
        handle = str(session_handle or "").strip()
        if not handle or self._persist_path is None:
            return None
        try:
            raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        closed = raw.get("closed_handles")
        if isinstance(closed, list) and handle in closed:
            return None
        items = raw.get("by_handle")
        if not isinstance(items, dict):
            return None
        meta = items.get(handle)
        return dict(meta) if isinstance(meta, dict) else None

    def lookup_persisted_by_session_id(self, session_id: str) -> dict[str, Any] | None:
        """按 session_id 只读磁盘索引（API 重启后 reattach／execute 补包名）。"""

        sid = str(session_id or "").strip()
        if not sid or self._persist_path is None:
            return None
        try:
            raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        closed = {
            str(item)
            for item in (raw.get("closed_handles") or [])
            if isinstance(item, str) and item.strip()
        }
        items = raw.get("by_handle")
        if not isinstance(items, dict):
            return None
        latest: dict[str, Any] | None = None
        latest_created = float("-inf")
        for meta in items.values():
            if not isinstance(meta, dict):
                continue
            handle = str(meta.get("session_handle") or "").strip()
            if handle and handle in closed:
                continue
            if str(meta.get("session_id") or "").strip() != sid:
                continue
            created = meta.get("created_at")
            created_at = float(created) if isinstance(created, (int, float)) else 0.0
            if latest is None or created_at >= latest_created:
                latest = dict(meta)
                latest_created = created_at
        return latest

    def require_by_handle(self, session_handle: str) -> XingyuSessionRecord:
        record = self.get_by_handle(session_handle)
        if record is None:
            raise KeyError(session_handle)
        return record

    def update(self, session_id: str, **fields: Any) -> XingyuSessionRecord:
        with self._lock:
            record = self._by_session.get(session_id)
            if record is None:
                raise KeyError(session_id)
            for key, value in fields.items():
                if not hasattr(record, key):
                    raise AttributeError(key)
                setattr(record, key, value)
            self._persist_locked()
            return record

    def pop(self, session_id: str) -> XingyuSessionRecord | None:
        with self._lock:
            record = self._by_session.pop(session_id, None)
            if record is not None:
                self._by_handle.pop(record.session_handle, None)
                self._tombstone_locked(record.session_handle)
                self._persist_locked()
            return record

    def clear(self) -> None:
        """仅清空内存；磁盘索引保留，供 API --reload 后恢复未关闭会话。"""
        with self._lock:
            self._by_session.clear()
            self._by_handle.clear()

    def _tombstone_locked(self, session_handle: str) -> None:
        """显式关闭后禁止按旧 handle 从磁盘复活。"""
        if self._persist_path is None:
            return
        handle = str(session_handle or "").strip()
        if not handle:
            return
        try:
            raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            raw = {"protocol_version": "xingyu-session-index.v1", "by_handle": {}}
        if not isinstance(raw, dict):
            raw = {"protocol_version": "xingyu-session-index.v1", "by_handle": {}}
        closed = raw.get("closed_handles")
        if not isinstance(closed, list):
            closed = []
        if handle not in closed:
            closed.append(handle)
        raw["closed_handles"] = closed[-200:]
        by_handle = raw.get("by_handle")
        if isinstance(by_handle, dict):
            by_handle.pop(handle, None)
            raw["by_handle"] = by_handle
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._persist_path)
        except OSError as exc:
            logger.warning(
                "xingyu session tombstone failed: %s",
                type(exc).__name__,
            )

    def _persist_locked(self) -> None:
        if self._persist_path is None:
            return
        closed: list[str] = []
        try:
            existing = json.loads(self._persist_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(
                existing.get("closed_handles"), list
            ):
                closed = [str(item) for item in existing["closed_handles"] if item]
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            closed = []
        closed_set = set(closed[-200:])
        payload = {
            "protocol_version": "xingyu-session-index.v1",
            "by_handle": {},
            "closed_handles": closed[-200:],
        }
        for record in self._by_session.values():
            if record.session_handle in closed_set:
                continue
            payload["by_handle"][record.session_handle] = {
                "session_id": record.session_id,
                "task_id": record.task_id,
                "session_handle": record.session_handle,
                "backend": record.backend,
                "serial": record.serial,
                "package_name": record.package_name,
                "activity": record.activity,
                "task_remark": record.task_remark,
                "morelogin_env_id": record.morelogin_env_id,
                "created_at": record.created_at,
            }
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._persist_path)
        except OSError as exc:
            logger.warning(
                "xingyu session index persist failed: %s",
                type(exc).__name__,
            )
