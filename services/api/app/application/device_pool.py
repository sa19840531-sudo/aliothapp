"""App 设备资源租约端口与本地兼容实现。

设备资源的生产事实由 PostgreSQL 实现，进程内实现只用于 Fake、本地开发和
没有显式开启生产设备池的旧调用。两个实现都遵守相同的 fencing、续租、释放、
恢复和隔离边界，避免旧 Worker 仅凭 session_id 释放新租约。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class DeviceLease:
    """已领取设备的公开租约身份；token 可用于 DB 续租但不进入跨模块协议。"""

    lease_id: str
    device_ref: str
    task_id: str
    session_id: str
    claimed_at: datetime
    expires_at: datetime
    backend: str
    profile_id: str
    lease_token: str = ""
    lease_owner: str = ""
    fencing_token: int = 0
    state: str = "claimed"
    last_renewed_at: datetime | None = None
    morelogin_env_id: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "lease_id",
            "device_ref",
            "task_id",
            "session_id",
            "backend",
            "profile_id",
            "state",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 必须为非空字符串")
        if self.state not in {
            "claimed",
            "active",
            "releasing",
            "recovering",
            "released",
            "quarantined",
        }:
            raise ValueError("state 不受支持")
        for field_name in ("claimed_at", "expires_at"):
            value = getattr(self, field_name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name} 必须带时区")
        if self.expires_at <= self.claimed_at:
            raise ValueError("expires_at 必须晚于 claimed_at")
        if self.lease_token and not isinstance(self.lease_token, str):
            raise ValueError("lease_token 必须为字符串")
        if (
            isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token < 0
        ):
            raise ValueError("fencing_token 必须为非负整数")
        env_id = self.morelogin_env_id.strip() if isinstance(self.morelogin_env_id, str) else ""
        object.__setattr__(self, "morelogin_env_id", env_id)


class DevicePoolError(RuntimeError):
    """设备池租约失败。"""

    def __init__(self, message: str, *, error_code: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class DeviceLeaseLostError(DevicePoolError):
    """当前 Worker 已失去设备 fencing，禁止继续调用 Driver。"""

    def __init__(self, message: str = "App 设备租约已失效") -> None:
        super().__init__(message, error_code="APP_DEVICE_LEASE_LOST", retryable=True)


class DeviceLeasePool(Protocol):
    """设备租约实现需要提供的异步事实源能力。"""

    async def claim(
        self,
        *,
        task_id: str,
        session_id: str,
        preferred_device_ref: str | None = None,
        lease_seconds: int = 3600,
        lease_owner: str | None = None,
    ) -> DeviceLease: ...

    async def renew(
        self,
        *,
        lease: DeviceLease,
        lease_seconds: int,
    ) -> DeviceLease: ...

    async def release(
        self,
        *,
        lease: DeviceLease,
    ) -> bool: ...

    async def quarantine(
        self,
        *,
        lease: DeviceLease,
        reason: str,
    ) -> bool: ...


@dataclass(frozen=True, kw_only=True)
class DeviceInventoryItem:
    """工作台设备库存只读投影：资源行 + 活跃租约 + 偏好排队观测。"""

    device_ref: str
    backend: str
    profile_id: str
    enabled: bool
    health_state: str
    occupied: bool
    occupancy_status: str
    lease_task_id: str | None = None
    lease_session_id: str | None = None
    lease_state: str | None = None
    lease_expires_at: datetime | None = None
    morelogin_env_id: str = ""
    queued_preferred_count: int = 0

    def __post_init__(self) -> None:
        required_fields = (
            "device_ref",
            "backend",
            "profile_id",
            "health_state",
            "occupancy_status",
        )
        for field_name in required_fields:
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 必须为非空字符串")
        if self.queued_preferred_count < 0:
            raise ValueError("queued_preferred_count 不能为负")


@dataclass(frozen=True, kw_only=True)
class DeviceSlotSeed:
    """库存槽位种子；cloud_phone 的 env_id／ADB 写入 capabilities。"""

    device_ref: str
    backend: str
    profile_id: str
    morelogin_env_id: str = ""
    vendor: str = ""

    def capabilities(self) -> dict[str, Any]:
        env_id = self.morelogin_env_id.strip()
        if self.backend != "cloud_phone" or not env_id:
            return {}
        vendor = (self.vendor or "").strip().lower()
        if not vendor:
            from services.api.app.infrastructure.device.cloud_phone_vendor import (
                looks_like_adb_endpoint,
                resolve_cloud_phone_vendor,
            )

            vendor = (
                "onirent"
                if looks_like_adb_endpoint(env_id)
                else resolve_cloud_phone_vendor()
            )
        return {"vendor": vendor, "env_id": env_id}


@dataclass
class _PoolSlot:
    device_ref: str
    backend: str
    profile_id: str
    fencing_epoch: int = 0
    lease: DeviceLease | None = None
    morelogin_env_id: str = ""


def _csv_items(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in str(raw or "").split(",") if item.strip())


def parse_device_pool_slot_seeds(
    slots: Sequence[str] | None = None,
    *,
    strict_env_mapping: bool = True,
) -> tuple[DeviceSlotSeed, ...]:
    """解析库存槽位与云手机绑定（ONIRENT ADB 或 MoreLogin env_id）等长映射。"""
    from services.api.app.infrastructure.device.cloud_phone_vendor import (
        list_pool_onirent_adb_endpoints,
        resolve_cloud_phone_vendor,
    )

    refs = list(slots) if slots is not None else list(
        _csv_items(os.getenv("ALIOTH_DEVICE_POOL_SLOTS", ""))
    )
    vendor = resolve_cloud_phone_vendor()
    # MoreLogin 路径不得触达 Onirent/CC 智控 WS；仅 onirent 供应商才解析 ADB 池。
    onirent_adbs: list[str] = []
    if vendor == "onirent":
        onirent_adbs = list_pool_onirent_adb_endpoints(allow_ws=strict_env_mapping)
        if not onirent_adbs:
            # 启动／非严格路径：WS 失败或延后时仍可用 env 备援
            onirent_adbs = list_pool_onirent_adb_endpoints(allow_ws=False)
    morelogin_ids = list(_csv_items(os.getenv("ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS", "")))
    if vendor == "onirent" and onirent_adbs:
        env_ids = onirent_adbs
        bind_vendor = "onirent"
        mapping_name = "ALIOTH_DEVICE_POOL_ONIRENT_ADB"
    else:
        env_ids = morelogin_ids
        bind_vendor = "morelogin"
        mapping_name = "ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS"
    global_id = (os.getenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID") or "").strip()
    parsed: list[DeviceSlotSeed] = []
    cloud_phone_indexes: list[int] = []
    for ref in refs:
        raw = str(ref or "").strip()
        if not raw:
            continue
        backend, _, profile = raw.partition(":")
        seed = DeviceSlotSeed(
            device_ref=raw,
            backend=backend or "cloud_phone",
            profile_id=profile or "default",
            vendor=bind_vendor,
        )
        parsed.append(seed)
        if seed.backend == "cloud_phone":
            cloud_phone_indexes.append(len(parsed) - 1)
    if env_ids and len(env_ids) != len(cloud_phone_indexes):
        if strict_env_mapping:
            raise DevicePoolError(
                f"{mapping_name} 必须与 cloud_phone 槽位等长",
                error_code="DEVICE_POOL_ENV_ID_MAPPING_INVALID",
                retryable=False,
            )
        env_ids = []
    if env_ids:
        for index, env_id in zip(cloud_phone_indexes, env_ids, strict=True):
            current = parsed[index]
            parsed[index] = DeviceSlotSeed(
                device_ref=current.device_ref,
                backend=current.backend,
                profile_id=current.profile_id,
                morelogin_env_id=env_id,
                vendor=bind_vendor,
            )
    elif bind_vendor == "morelogin" and len(cloud_phone_indexes) == 1 and global_id:
        current = parsed[cloud_phone_indexes[0]]
        parsed[cloud_phone_indexes[0]] = DeviceSlotSeed(
            device_ref=current.device_ref,
            backend=current.backend,
            profile_id=current.profile_id,
            morelogin_env_id=global_id,
            vendor="morelogin",
        )
    return tuple(parsed)


def morelogin_env_ids_by_device_ref(
    slots: Sequence[str] | None = None,
) -> dict[str, str]:
    """按 device_ref 返回已映射的 MoreLogin env_id。"""
    mapping: dict[str, str] = {}
    for seed in parse_device_pool_slot_seeds(slots, strict_env_mapping=False):
        if seed.morelogin_env_id:
            mapping[seed.device_ref] = seed.morelogin_env_id
    return mapping


def count_cloud_phone_slots(slots: Sequence[str] | None = None) -> int:
    """统计 cloud_phone 库存台数，用于判断是否允许全域单机回退。"""
    return sum(
        1
        for seed in parse_device_pool_slot_seeds(slots, strict_env_mapping=False)
        if seed.backend == "cloud_phone"
    )


def allow_global_morelogin_fallback(slots: Sequence[str] | None = None) -> bool:
    """仅当库存不超过 1 台 cloud_phone 时，允许读全域 CLOUDPHONE_ID。"""
    return count_cloud_phone_slots(slots) <= 1


def resolve_morelogin_env_id_for_lease(lease: DeviceLease) -> str:
    """租约绑定优先；单机且 capabilities 缺 env_id 时才回退全域 ID。"""
    requested = (lease.morelogin_env_id or "").strip()
    if requested:
        return requested
    if lease.backend != "cloud_phone":
        return ""
    if not allow_global_morelogin_fallback():
        return ""
    return (os.getenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID") or "").strip()


class InProcessDevicePool:
    """单进程兼容设备池；生产跨 Worker 需使用 PostgresDevicePool。"""

    def __init__(self, slots: list[str] | None = None) -> None:
        refs = list(slots or ["cloud_phone:default", "ldplayer:0", "google_emulator:default"])
        self._lock = threading.RLock()
        self._slots: dict[str, _PoolSlot] = {}
        env_by_ref = morelogin_env_ids_by_device_ref(refs)
        for ref in refs:
            self._add_slot_unlocked(ref, morelogin_env_id=env_by_ref.get(ref, ""))

    def configure_slots(self, slots: list[str]) -> None:
        with self._lock:
            env_by_ref = morelogin_env_ids_by_device_ref(list(self._slots) + list(slots))
            for ref in slots:
                if ref not in self._slots:
                    self._add_slot_unlocked(
                        ref,
                        morelogin_env_id=env_by_ref.get(ref, ""),
                    )

    def claim(
        self,
        *,
        task_id: str,
        session_id: str,
        preferred_device_ref: str | None = None,
        lease_seconds: int = 3600,
        lease_owner: str | None = None,
    ) -> DeviceLease:
        _validate_lease_seconds(lease_seconds)
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=lease_seconds)
        preferred = str(preferred_device_ref or "").strip() or None
        with self._lock:
            self._expire_unlocked(now)
            existing = next(
                (
                    slot.lease
                    for slot in self._slots.values()
                    if slot.lease is not None
                    and slot.lease.task_id == task_id
                    and slot.lease.session_id == session_id
                ),
                None,
            )
            if existing is not None:
                if (
                    existing.lease_owner == str(lease_owner or f"device-claim:{session_id}")
                    and existing.state in {"claimed", "active"}
                    and existing.expires_at > now
                ):
                    return existing
                raise DevicePoolError(
                    "当前 App Session 已绑定其他设备租约或进入恢复状态",
                    error_code="DEVICE_LEASE_ALREADY_OWNED",
                )
            if preferred:
                preferred_slot = self._slots.get(preferred)
                if preferred_slot is None:
                    raise DevicePoolError(
                        "指定 App 设备资源不存在",
                        error_code="DEVICE_RESOURCE_NOT_FOUND",
                        retryable=False,
                    )
                if preferred_slot.lease is None:
                    ordered = [preferred_slot]
                else:
                    # 指定设备占用时改领其他空闲槽位，避免在同一台设备上一直等到超时。
                    ordered = [
                        slot
                        for _, slot in sorted(self._slots.items())
                        if slot.device_ref != preferred and slot.lease is None
                    ]
                    if not ordered:
                        raise DevicePoolError(
                            "指定 App 设备资源正在使用，且没有其他空闲设备",
                            error_code="DEVICE_BUSY",
                        )
            else:
                ordered = list(self._slots.values())
            for slot in ordered:
                if slot.lease is not None:
                    continue
                slot.fencing_epoch += 1
                lease = DeviceLease(
                    lease_id=str(uuid.uuid4()),
                    device_ref=slot.device_ref,
                    task_id=task_id,
                    session_id=session_id,
                    claimed_at=now,
                    expires_at=expires,
                    backend=slot.backend,
                    profile_id=slot.profile_id,
                    lease_token=str(uuid.uuid4()),
                    lease_owner=str(lease_owner or f"device-claim:{session_id}"),
                    fencing_token=slot.fencing_epoch,
                    state="claimed",
                    morelogin_env_id=slot.morelogin_env_id,
                )
                slot.lease = lease
                return lease
            raise DevicePoolError("无可用设备", error_code="DEVICE_POOL_EXHAUSTED")

    def renew(
        self,
        *,
        lease: DeviceLease,
        lease_seconds: int,
    ) -> DeviceLease:
        _validate_lease_seconds(lease_seconds)
        now = datetime.now(UTC)
        with self._lock:
            slot = self._slots.get(lease.device_ref)
            current = slot.lease if slot is not None else None
            if not _lease_identity_matches(current, lease) or current is None:
                raise DeviceLeaseLostError()
            if current.state == "recovering":
                pass
            elif current.state not in {"claimed", "active"} or current.expires_at <= now:
                raise DeviceLeaseLostError()
            renewed = replace(
                current,
                expires_at=now + timedelta(seconds=lease_seconds),
                last_renewed_at=now,
                state="active",
            )
            slot.lease = renewed
            return renewed

    def release(
        self,
        *,
        lease_id: str | None = None,
        session_id: str | None = None,
        lease: DeviceLease | None = None,
    ) -> bool:
        """释放健康关闭的租约；带 lease 时强制校验 owner/token/fencing。"""
        if lease is None:
            return False
        with self._lock:
            # 过期租约先进入 recovering，不能因为迟到的 Closure 误把设备标成可用。
            self._expire_unlocked(datetime.now(UTC))
            for slot in self._slots.values():
                current = slot.lease
                if current is None:
                    continue
                if not _lease_identity_matches(current, lease):
                    continue
                if current.state not in {"claimed", "active", "releasing"}:
                    return False
                slot.lease = None
                return True
            return False

    def recover(self, *, lease: DeviceLease) -> DeviceLease:
        """把过期或待关闭租约标为 recovering，阻止未经验证的复用。"""
        with self._lock:
            slot = self._slots.get(lease.device_ref)
            current = slot.lease if slot is not None else None
            if not _lease_identity_matches(current, lease) or current is None:
                raise DeviceLeaseLostError()
            if current.state not in {"claimed", "active", "releasing"}:
                return current
            recovered = replace(current, state="recovering")
            slot.lease = recovered
            return recovered

    def quarantine(self, *, lease: DeviceLease, reason: str) -> bool:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须为非空字符串")
        with self._lock:
            slot = self._slots.get(lease.device_ref)
            current = slot.lease if slot is not None else None
            if not _lease_identity_matches(current, lease) or current is None:
                return False
            if current.state == "quarantined":
                return True
            slot.lease = replace(current, state="quarantined")
            return True

    async def async_claim(self, **kwargs: Any) -> DeviceLease:
        return self.claim(**kwargs)

    async def async_renew(self, *, lease: DeviceLease, lease_seconds: int) -> DeviceLease:
        return self.renew(lease=lease, lease_seconds=lease_seconds)

    async def async_release(self, *, lease: DeviceLease) -> bool:
        return self.release(lease=lease)

    async def async_recover(self, *, lease: DeviceLease) -> DeviceLease:
        return self.recover(lease=lease)

    async def async_quarantine(self, *, lease: DeviceLease, reason: str) -> bool:
        return self.quarantine(lease=lease, reason=reason)

    def get(
        self,
        *,
        lease_id: str | None = None,
        session_id: str | None = None,
    ) -> DeviceLease | None:
        with self._lock:
            for slot in self._slots.values():
                if slot.lease is None:
                    continue
                if lease_id and slot.lease.lease_id == lease_id:
                    return slot.lease
                if session_id and slot.lease.session_id == session_id:
                    return slot.lease
            return None

    async def async_get(
        self,
        *,
        lease_id: str | None = None,
        session_id: str | None = None,
    ) -> DeviceLease | None:
        return self.get(lease_id=lease_id, session_id=session_id)

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._expire_unlocked(datetime.now(UTC))
            return {
                "total": len(self._slots),
                "available": sum(1 for slot in self._slots.values() if slot.lease is None),
                "leased": [
                    {
                        "device_ref": slot.device_ref,
                        "lease_id": slot.lease.lease_id if slot.lease else None,
                        "session_id": slot.lease.session_id if slot.lease else None,
                        "state": slot.lease.state if slot.lease else None,
                        "fencing_token": slot.lease.fencing_token if slot.lease else None,
                    }
                    for slot in self._slots.values()
                    if slot.lease is not None
                ],
            }

    def _add_slot_unlocked(self, ref: str, *, morelogin_env_id: str = "") -> None:
        backend, _, profile = ref.partition(":")
        self._slots[ref] = _PoolSlot(
            device_ref=ref,
            backend=backend or "cloud_phone",
            profile_id=profile or "default",
            morelogin_env_id=str(morelogin_env_id or "").strip(),
        )

    def recover_expired(self, *, limit: int = 100) -> int:
        """把过期 claimed/active 标成 recovering，设备不会被静默复用。"""
        del limit
        with self._lock:
            before = [
                slot.lease
                for slot in self._slots.values()
                if slot.lease is not None and slot.lease.state == "recovering"
            ]
            self._expire_unlocked(datetime.now(UTC))
            after = [
                slot.lease
                for slot in self._slots.values()
                if slot.lease is not None and slot.lease.state == "recovering"
            ]
            return max(0, len(after) - len(before))

    async def async_recover_expired(self) -> int:
        return self.recover_expired()

    def _expire_unlocked(self, now: datetime) -> None:
        # 到期后进入 recovering，只有显式 release/quarantine 才能改变资源占用。
        for slot in self._slots.values():
            current = slot.lease
            if current is not None and current.expires_at <= now:
                if current.state in {"claimed", "active", "releasing"}:
                    slot.lease = replace(current, state="recovering")


class PostgresDevicePool:
    """生产设备池：PostgreSQL 为唯一事实源，claim 前先做过期回收。"""

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    async def configure_resources(
        self,
        resources: Any | None = None,
    ) -> int:
        return await self._repository.configure_resources(resources)

    async def claim(
        self,
        *,
        task_id: str,
        session_id: str,
        preferred_device_ref: str | None = None,
        lease_seconds: int = 3600,
        lease_owner: str | None = None,
    ) -> DeviceLease:
        # 过期占用先进入 recovering；终态 Session 占用立即腾出，避免池被僵尸租约填满。
        await self._repository.recover_expired()
        await self._repository.reclaim_recovering()
        await self._repository.release_finished_task_devices()
        return await self._repository.claim(
            task_id=task_id,
            session_id=session_id,
            preferred_device_ref=preferred_device_ref,
            lease_seconds=lease_seconds,
            lease_owner=lease_owner,
        )

    async def renew(self, *, lease: DeviceLease, lease_seconds: int) -> DeviceLease:
        return await self._repository.renew(lease=lease, lease_seconds=lease_seconds)

    async def release(self, *, lease: DeviceLease) -> bool:
        return await self._repository.release(lease=lease)

    async def recover(self, *, lease: DeviceLease) -> DeviceLease:
        return await self._repository.recover(lease=lease)

    async def recover_then_handoff(
        self,
        *,
        lease: DeviceLease,
        successor_session_id: str,
        predecessor_channel_silenced: bool = True,
    ) -> DeviceLease:
        """应用层薄包装：recover → handoff_to_successor_session。"""

        recover_then = getattr(self._repository, "recover_then_handoff", None)
        if callable(recover_then):
            return await recover_then(
                lease=lease,
                successor_session_id=successor_session_id,
                predecessor_channel_silenced=predecessor_channel_silenced,
            )
        recovered = await self._repository.recover(lease=lease)
        return await self._repository.handoff_to_successor_session(
            predecessor=recovered,
            successor_session_id=successor_session_id,
            predecessor_channel_silenced=predecessor_channel_silenced,
        )

    async def quarantine(self, *, lease: DeviceLease, reason: str) -> bool:
        return await self._repository.quarantine(lease=lease, reason=reason)

    async def get(
        self,
        *,
        lease_id: str | None = None,
        session_id: str | None = None,
    ) -> DeviceLease | None:
        return await self._repository.get(lease_id=lease_id, session_id=session_id)

    async def get_active(
        self,
        *,
        lease_id: str | None = None,
        session_id: str | None = None,
    ) -> DeviceLease | None:
        return await self.get(lease_id=lease_id, session_id=session_id)

    async def recover_expired(self, *, limit: int = 100) -> int:
        return await self._repository.recover_expired(limit=limit)

    async def reclaim_recovering(self, *, limit: int = 100) -> int:
        return await self._repository.reclaim_recovering(limit=limit)

    async def release_finished_task_devices(self, *, limit: int = 100) -> int:
        return await self._repository.release_finished_task_devices(limit=limit)

    async def quarantine_stale_recovering(
        self,
        *,
        grace_seconds: int = 60,
        limit: int = 100,
    ) -> int:
        return await self._repository.quarantine_stale_recovering(
            grace_seconds=grace_seconds,
            limit=limit,
        )


class DeviceLeaseGuard:
    """在 App 外部动作期间持续续租并把旧 Worker 隔离成可检查错误。"""

    def __init__(
        self,
        *,
        pool: DeviceLeasePool,
        lease: DeviceLease,
        lease_seconds: int,
        renew_interval_seconds: int | None = None,
    ) -> None:
        _validate_lease_seconds(lease_seconds)
        self._pool = pool
        self.lease = lease
        self._lease_seconds = lease_seconds
        self._renew_interval_seconds = max(
            1,
            int(renew_interval_seconds or max(1, lease_seconds // 3)),
        )
        self._renew_task: asyncio.Task[None] | None = None
        self._renew_failure: BaseException | None = None
        self._renew_failure_event = asyncio.Event()

    async def __aenter__(self) -> DeviceLeaseGuard:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        await self.stop()

    async def start(self) -> None:
        if self._renew_task is not None:
            await self.assert_healthy()
            return
        try:
            self.lease = await _await_if_needed(
                self._pool.renew(
                    lease=self.lease,
                    lease_seconds=self._lease_seconds,
                )
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            self._renew_failure = exc
            self._renew_failure_event.set()
            await self.assert_healthy()
        self._renew_task = asyncio.create_task(
            self._renew_forever(),
            name="app-device-lease-renew",
        )
        await self.assert_healthy()

    async def assert_healthy(self) -> None:
        if self._renew_failure is not None:
            raise DeviceLeaseLostError() from self._renew_failure

    async def run(self, operation: Any) -> Any:
        """续租失效时取消外部操作，避免旧 Worker 继续触碰设备。"""
        await self.assert_healthy()
        operation_task = asyncio.ensure_future(operation)
        lost_task = asyncio.create_task(self._renew_failure_event.wait())
        try:
            done, _ = await asyncio.wait(
                {operation_task, lost_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lost_task in done:
                operation_task.cancel()
                try:
                    await operation_task
                except asyncio.CancelledError:
                    pass
                await self.assert_healthy()
            result = await operation_task
            await self.assert_healthy()
            return result
        finally:
            lost_task.cancel()
            try:
                await lost_task
            except asyncio.CancelledError:
                pass

    async def stop(self) -> None:
        task = self._renew_task
        self._renew_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.assert_healthy()

    async def _renew_forever(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._renew_interval_seconds)
                self.lease = await _await_if_needed(
                    self._pool.renew(
                        lease=self.lease,
                        lease_seconds=self._lease_seconds,
                    )
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            self._renew_failure = exc
            self._renew_failure_event.set()


_POOL = InProcessDevicePool()
_POSTGRES_POOL: DevicePool | None = None
_POSTGRES_POOL_LOCK = asyncio.Lock()


def get_device_pool() -> InProcessDevicePool:
    """返回旧同步调用使用的进程内池。"""
    return _POOL


def reset_device_pool_for_tests(slots: list[str] | None = None) -> InProcessDevicePool:
    global _POOL, _POSTGRES_POOL
    _POOL = InProcessDevicePool(slots=slots)
    _POSTGRES_POOL = None
    return _POOL


def is_postgres_device_lease_backend() -> bool:
    """生产跨 Worker 互斥必须走 PostgreSQL 设备租约。"""
    provider = os.getenv(
        "ALIOTH_APP_DEVICE_LEASE_BACKEND",
        os.getenv("ALIOTH_DEVICE_POOL_PROVIDER", "in_process"),
    ).strip().casefold()
    return provider in {"postgres", "postgresql", "database"}


async def get_runtime_device_pool() -> DeviceLeasePool:
    """按显式配置选择生产 PostgreSQL 池，默认保持本地兼容实现。"""
    global _POSTGRES_POOL
    if is_postgres_device_lease_backend():
        if _POSTGRES_POOL is None:
            async with _POSTGRES_POOL_LOCK:
                if _POSTGRES_POOL is None:
                    from services.api.app.infrastructure.db.device_lease_repository import (
                        PostgresDeviceLeaseRepository,
                    )

                    repository = PostgresDeviceLeaseRepository()
                    pool = PostgresDevicePool(repository)
                    await pool.configure_resources()
                    _POSTGRES_POOL = pool
        return _POSTGRES_POOL
    provider = os.getenv(
        "ALIOTH_APP_DEVICE_LEASE_BACKEND",
        os.getenv("ALIOTH_DEVICE_POOL_PROVIDER", "in_process"),
    ).strip().casefold()
    if provider not in {"in_process", "local", "fake"}:
        raise DevicePoolError(
            f"不支持的 App 设备池 provider：{provider}",
            error_code="DEVICE_POOL_PROVIDER_INVALID",
            retryable=False,
        )
    return _POOL


async def claim_device_lease(
    *,
    task_id: str,
    session_id: str,
    preferred_device_ref: str | None,
    lease_seconds: int,
    lease_owner: str | None = None,
) -> DeviceLease:
    pool = await get_runtime_device_pool()
    result = pool.claim(
        task_id=task_id,
        session_id=session_id,
        preferred_device_ref=preferred_device_ref,
        lease_seconds=lease_seconds,
        lease_owner=lease_owner,
    )
    return await _await_if_needed(result)


async def renew_device_lease(
    *,
    pool: DeviceLeasePool,
    lease: DeviceLease,
    lease_seconds: int,
) -> DeviceLease:
    result = pool.renew(lease=lease, lease_seconds=lease_seconds)
    return await _await_if_needed(result)


async def release_device_lease(*, lease: DeviceLease) -> bool:
    pool = await get_runtime_device_pool()
    result = pool.release(lease=lease)
    released = bool(await _await_if_needed(result))
    if released:
        await maybe_cleanup_adb_after_app_qc(lease=lease, reason="lease_released")
        await maybe_power_off_cloud_phone_for_lease(lease=lease, reason="lease_released")
    return released


async def _has_other_active_device_leases(
    *,
    lease: DeviceLease | None = None,
    settled_env_id: str = "",
    settled_device_ref: str = "",
) -> bool:
    """判断池内是否仍有其他活跃租约（用于决定 ADB 清理是否杀进程）。"""

    env_id = settled_env_id or (lease.morelogin_env_id if lease is not None else "")
    device_ref = settled_device_ref or (lease.device_ref if lease is not None else "")
    active = await _active_morelogin_env_ids_excluding(
        settled_env_id=str(env_id or "").strip(),
        settled_device_ref=str(device_ref or "").strip(),
    )
    return bool(active)


async def maybe_cleanup_adb_after_app_qc(
    *,
    lease: DeviceLease,
    reason: str,
) -> None:
    """App 质检租约结束后清理本机 ADB；有并行租约时只 disconnect 不杀进程。"""

    from services.api.app.infrastructure.device.adb_process_cleanup import (
        adb_cleanup_after_qc_enabled,
        cleanup_adb_processes_after_app_qc,
    )

    if not adb_cleanup_after_qc_enabled():
        return
    endpoint = str(lease.morelogin_env_id or "").strip()
    if not endpoint:
        return
    other_active = await _has_other_active_device_leases(lease=lease)
    logs = cleanup_adb_processes_after_app_qc(
        endpoint=endpoint,
        kill_processes=not other_active,
        reason=reason or "app_qc",
    )
    for line in logs:
        logger.info("adb_cleanup task=%s %s", lease.task_id, line)


async def release_device_lease_for_session(*, session_id: str) -> bool:
    """按持久化 Session 绑定释放健康关闭的设备租约。"""
    lease = await get_device_lease(session_id=session_id)
    if lease is None:
        return False
    return await release_device_lease(lease=lease)


async def quarantine_device_lease(*, lease: DeviceLease, reason: str) -> bool:
    pool = await get_runtime_device_pool()
    result = pool.quarantine(lease=lease, reason=reason)
    quarantined = bool(await _await_if_needed(result))
    if quarantined:
        await maybe_power_off_cloud_phone_for_lease(
            lease=lease,
            reason=f"lease_quarantined:{reason}",
        )
    return quarantined


async def quarantine_device_lease_for_session(*, session_id: str, reason: str) -> bool:
    """按持久化 Session 绑定隔离未确认关闭的设备。"""
    lease = await get_device_lease(session_id=session_id)
    if lease is None:
        return False
    return await quarantine_device_lease(lease=lease, reason=reason)


async def maybe_power_off_cloud_phone_for_lease(
    *,
    lease: DeviceLease,
    reason: str,
) -> None:
    """App 质检租约结算后关闭本台，并关闭池内其余空闲云手机（省计费）。

    覆盖 ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS 映射的全部 cloud_phone（qa-a～qa-j）。
    仍有活跃租约的机器跳过，避免误关并发任务。
    若仍有排队／待领取设备的 App 任务，整次跳过关机以便连跑。
    开关 ``ALIOTH_MORELOGIN_ADB_AUTO_POWER_OFF=0`` 可在测试阶段彻底关闭。
    失败只记日志。
    """
    if lease.backend != "cloud_phone":
        return
    env_id = resolve_morelogin_env_id_for_lease(lease)
    try:
        from services.api.app.infrastructure.device.morelogin_adb import (
            maybe_power_off_morelogin_after_lease,
            maybe_power_off_morelogin_fleet_idle,
            morelogin_adb_auto_power_off_enabled,
        )

        if not morelogin_adb_auto_power_off_enabled():
            logger.info(
                "cloud_phone_auto_power_off: disabled",
                extra={
                    "device_ref": lease.device_ref,
                    "profile_id": lease.profile_id,
                    "task_id": lease.task_id,
                    "session_id": lease.session_id,
                    "morelogin_env_id": env_id,
                    "reason": reason,
                },
            )
            return

        pending = await _count_pending_app_device_demand(
            exclude_task_id=lease.task_id,
        )
        if pending > 0:
            logger.info(
                "cloud_phone_auto_power_off: deferred pending_demand=%s",
                pending,
                extra={
                    "device_ref": lease.device_ref,
                    "profile_id": lease.profile_id,
                    "task_id": lease.task_id,
                    "session_id": lease.session_id,
                    "morelogin_env_id": env_id,
                    "reason": reason,
                    "pending_demand": pending,
                },
            )
            return

        logs = maybe_power_off_morelogin_after_lease(
            backend=lease.backend,
            morelogin_env_id=env_id,
            reason=reason,
        )
        # 本台已结算；其余仍占用的 envId 保留开机，其余全部关机。
        skip = await _active_morelogin_env_ids_excluding(
            settled_env_id=env_id,
            settled_device_ref=lease.device_ref,
        )
        logs.extend(
            maybe_power_off_morelogin_fleet_idle(
                skip_env_ids=skip,
                reason=f"fleet_after:{reason}",
            )
        )
        for line in logs:
            logger.info(
                "cloud_phone_auto_power_off: %s",
                line,
                extra={
                    "device_ref": lease.device_ref,
                    "profile_id": lease.profile_id,
                    "task_id": lease.task_id,
                    "session_id": lease.session_id,
                    "morelogin_env_id": env_id,
                    "reason": reason,
                },
            )
    except Exception:  # noqa: BLE001 - 关机失败不得回滚租约
        logger.exception(
            "cloud_phone_auto_power_off_failed",
            extra={
                "device_ref": lease.device_ref,
                "profile_id": lease.profile_id,
                "task_id": lease.task_id,
                "session_id": lease.session_id,
                "morelogin_env_id": env_id,
                "reason": reason,
            },
        )


async def _count_pending_app_device_demand(*, exclude_task_id: str | None) -> int:
    """读取仍需设备的 App 任务数；仓储不可用或查询失败时视为 0（倾向关机省计费）。"""
    try:
        pool = await get_runtime_device_pool()
    except Exception:  # noqa: BLE001
        return 0
    repository = getattr(pool, "_repository", None)
    counter = getattr(repository, "count_pending_app_device_demand", None)
    if counter is None:
        return 0
    try:
        return max(0, int(await counter(exclude_task_id=exclude_task_id)))
    except Exception:  # noqa: BLE001
        logger.exception(
            "cloud_phone_pending_demand_count_failed",
            extra={"exclude_task_id": exclude_task_id},
        )
        return 0


async def _active_morelogin_env_ids_excluding(
    *,
    settled_env_id: str,
    settled_device_ref: str,
) -> set[str]:
    """收集仍有活跃租约的云手机 envId（不含刚结算的本台）。"""
    active: set[str] = set()
    settled = str(settled_env_id or "").strip()
    settled_ref = str(settled_device_ref or "").strip()
    try:
        pool = await get_runtime_device_pool()
    except Exception:  # noqa: BLE001
        return active

    repository = getattr(pool, "_repository", None)
    if repository is not None and hasattr(repository, "list_inventory"):
        try:
            items = await repository.list_inventory()
        except Exception:  # noqa: BLE001
            items = []
        for item in items:
            if getattr(item, "backend", "") != "cloud_phone":
                continue
            if str(getattr(item, "device_ref", "") or "").strip() == settled_ref:
                continue
            lease_state = str(getattr(item, "lease_state", "") or "").strip()
            if lease_state not in {"claimed", "active", "releasing", "recovering"}:
                continue
            eid = str(getattr(item, "morelogin_env_id", "") or "").strip()
            if eid:
                active.add(eid)
        return active

    slots = getattr(pool, "_slots", None)
    if isinstance(slots, dict):
        for slot in slots.values():
            if getattr(slot, "backend", "") != "cloud_phone":
                continue
            if str(getattr(slot, "device_ref", "") or "").strip() == settled_ref:
                continue
            lease = getattr(slot, "lease", None)
            if lease is None:
                continue
            state = str(getattr(lease, "state", "") or "").strip()
            if state not in {"claimed", "active", "releasing", "recovering"}:
                continue
            eid = str(
                getattr(lease, "morelogin_env_id", "")
                or getattr(slot, "morelogin_env_id", "")
                or ""
            ).strip()
            if eid:
                active.add(eid)
    if settled:
        active.discard(settled)
    return active


async def get_device_lease(
    *,
    lease_id: str | None = None,
    session_id: str | None = None,
) -> DeviceLease | None:
    pool = await get_runtime_device_pool()
    getter = getattr(pool, "get", None) or getattr(pool, "get_active", None)
    if not callable(getter):
        return None
    result = getter(lease_id=lease_id, session_id=session_id)
    return await _await_if_needed(result)


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _lease_identity_matches(current: DeviceLease | None, requested: DeviceLease) -> bool:
    if current is None:
        return False
    if current.lease_id != requested.lease_id:
        return False
    if requested.lease_token and current.lease_token != requested.lease_token:
        return False
    if requested.fencing_token and current.fencing_token != requested.fencing_token:
        return False
    if requested.lease_owner and current.lease_owner != requested.lease_owner:
        return False
    return current.task_id == requested.task_id and current.session_id == requested.session_id


def _validate_lease_seconds(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 60:
        raise ValueError("lease_seconds 必须为不小于 60 的整数")


# 供类型检查和运行时延迟导入使用，避免 application → infrastructure 循环导入。
DevicePool = DeviceLeasePool
