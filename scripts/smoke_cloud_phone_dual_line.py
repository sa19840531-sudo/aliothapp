#!/usr/bin/env python3
"""第 58 行实连冒烟：2 云手机 Open API + 租约 env 选机 + 可区分截图。

用法（会 powerOn，会计费）：
  python scripts/smoke_cloud_phone_dual_line.py

默认读取仓库根 `.env` / `.env.local` 与 `infra/env/.env.local`。
不把 API Key 写入日志。截图落到 `.alioth-mobile-demo/dual-line-smoke/`（gitignore）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _load_env_files() -> None:
    from dotenv import dotenv_values

    protected = set(os.environ)
    for path in (ROOT / ".env", ROOT / ".env.local", ROOT / "infra" / "env" / ".env.local"):
        if not path.is_file():
            continue
        for key, value in dotenv_values(path).items():
            if value is None or key in protected:
                continue
            os.environ[key] = value
            protected.add(key)


_load_env_files()


@dataclass(frozen=True, kw_only=True)
class LiveLine:
    """一条实连线的可观测结果。"""

    slot: str
    env_id: str
    endpoint: str
    serial: str
    android_id: str
    screenshot: str
    screenshot_sha256: str


def _csv(name: str) -> list[str]:
    return [item.strip() for item in (os.getenv(name) or "").split(",") if item.strip()]


def _is_localhost_endpoint(endpoint: str) -> bool:
    host = (endpoint or "").split(":", 1)[0].strip().lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def _tcp_reachable(endpoint: str, *, timeout_seconds: float = 3.0) -> bool:
    if ":" not in endpoint:
        return False
    host, port_raw = endpoint.rsplit(":", 1)
    try:
        port = int(port_raw)
    except ValueError:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def _preflight_adb_endpoints(env_ids: list[str]) -> list[dict[str, str]]:
    from services.api.app.infrastructure.device import morelogin_adb as ml

    rows: list[dict[str, str]] = []
    for env_id in env_ids:
        info, _logs = ml.refresh_cloud_phone_adb_from_morelogin(
            env_id,
            apply_to_process_env=False,
            allow_global_fallback=False,
        )
        if info is None:
            raise RuntimeError(f"envId={env_id} 未返回 ADB 端点")
        endpoint = info.endpoint
        row = {
            "env_id": env_id,
            "endpoint": endpoint,
            "env_name": info.env_name or "",
        }
        if _is_localhost_endpoint(endpoint) and not _tcp_reachable(endpoint):
            row["blocked"] = (
                "MoreLogin 返回 localhost ADB，但本机隧道未监听。"
                "请启动 MoreLogin 客户端，或在控制台为该机开启公网 ADB。"
            )
        rows.append(row)
    return rows


def _require_live_config() -> tuple[list[str], list[str]]:
    slots = [item for item in _csv("ALIOTH_DEVICE_POOL_SLOTS") if item.startswith("cloud_phone:")]
    env_ids = _csv("ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS")
    api_id = (os.getenv("ALIOTH_MORELOGIN_API_ID") or "").strip()
    api_key = (os.getenv("ALIOTH_MORELOGIN_API_KEY") or "").strip()
    if len(slots) < 2 or len(env_ids) < 2:
        raise SystemExit(
            "需要至少 2 个 cloud_phone 槽位与等长 ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS"
        )
    if len(slots) != len(env_ids):
        raise SystemExit("ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS 必须与 cloud_phone 槽位等长")
    if not api_id or not api_key:
        raise SystemExit("缺少 ALIOTH_MORELOGIN_API_ID / ALIOTH_MORELOGIN_API_KEY，无法打 Open API")
    os.environ.setdefault("ALIOTH_MORELOGIN_ADB_AUTO_REFRESH", "1")
    os.environ.setdefault("ALIOTH_MORELOGIN_ADB_AUTO_ENABLE", "1")
    os.environ.setdefault("ALIOTH_MORELOGIN_ADB_AUTO_POWER_ON", "1")
    return slots[:2], env_ids[:2]


def _bring_up_line(slot: str, env_id: str, shots_dir: Path) -> LiveLine:
    from services.api.app.infrastructure.device.adb_ops import open_adb_device_ops
    from services.api.app.infrastructure.device.backend import BACKEND_CLOUD_PHONE
    from services.api.app.infrastructure.device.cloud_phone import CloudPhoneController

    process_adb_before = os.getenv("ALIOTH_CLOUD_PHONE_ADB")
    controller = CloudPhoneController(
        morelogin_env_id=env_id,
        allow_global_morelogin_fallback=False,
        apply_morelogin_to_process_env=False,
    )
    serial, logs = controller.ensure_ready()
    for line in logs[-8:]:
        print(f"  [{slot}] {line}")
    if os.getenv("ALIOTH_CLOUD_PHONE_ADB") != process_adb_before:
        raise RuntimeError("实连冒烟禁止把租约 ADB 写回进程环境，以免踩另一台")
    ops = open_adb_device_ops(
        BACKEND_CLOUD_PHONE,
        log=lambda msg: print(f"  [{slot}] {msg}"),
        cloud_phone_controller=controller,
    )
    android_id = (
        ops.shell_output(["settings", "get", "secure", "android_id"], timeout=20.0) or ""
    ).strip()
    shot = shots_dir / f"{slot.replace(':', '_')}_{env_id}.png"
    ops.screencap(shot)
    digest = hashlib.sha256(shot.read_bytes()).hexdigest()
    endpoint = str(getattr(controller, "_endpoint", "") or serial)
    return LiveLine(
        slot=slot,
        env_id=env_id,
        endpoint=endpoint,
        serial=serial,
        android_id=android_id,
        screenshot=str(shot),
        screenshot_sha256=digest,
    )


async def _smoke_in_process_pool(slots: list[str], env_ids: list[str]) -> dict[str, object]:
    from services.api.app.application.device_pool import (
        DevicePoolError,
        reset_device_pool_for_tests,
    )

    os.environ["ALIOTH_DEVICE_POOL_SLOTS"] = ",".join(slots)
    os.environ["ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS"] = ",".join(env_ids)
    pool = reset_device_pool_for_tests(slots=slots)
    first = pool.claim(task_id=str(uuid4()), session_id="live-session-1", lease_seconds=120)
    second = pool.claim(task_id=str(uuid4()), session_id="live-session-2", lease_seconds=120)
    if first.morelogin_env_id == second.morelogin_env_id:
        raise RuntimeError("进程内双 claim 得到相同 env_id")
    try:
        pool.claim(task_id=str(uuid4()), session_id="live-session-3", lease_seconds=120)
        raise RuntimeError("池满第三人应当失败，却领取到租约")
    except DevicePoolError as exc:
        if exc.error_code != "DEVICE_POOL_EXHAUSTED":
            raise
    released = pool.release(lease=first) and pool.release(lease=second)
    return {
        "mutex_env_ids": sorted({first.morelogin_env_id, second.morelogin_env_id}),
        "third_timeout": "DEVICE_POOL_EXHAUSTED",
        "released": bool(released),
        "available_after_release": pool.status()["available"],
    }


async def _smoke_postgres_pool() -> dict[str, object] | None:
    if (os.getenv("ALIOTH_APP_DEVICE_LEASE_BACKEND") or "").strip().casefold() not in {
        "postgres",
        "postgresql",
        "database",
    }:
        return None
    if not (os.getenv("ALIOTH_DATABASE_URL") or "").strip():
        return None
    from services.api.app.application.device_pool import DevicePoolError, PostgresDevicePool
    from services.api.app.infrastructure.db.device_lease_repository import (
        PostgresDeviceLeaseRepository,
    )

    repository = PostgresDeviceLeaseRepository()
    pool = PostgresDevicePool(repository)
    seeded = await pool.configure_resources()
    owner = f"dual-line-smoke:{uuid4().hex[:8]}"
    first = second = None
    try:
        first, second = await asyncio.gather(
            pool.claim(
                task_id=str(uuid4()),
                session_id=f"pg-live-1-{uuid4().hex[:8]}",
                lease_seconds=120,
                lease_owner=owner,
            ),
            pool.claim(
                task_id=str(uuid4()),
                session_id=f"pg-live-2-{uuid4().hex[:8]}",
                lease_seconds=120,
                lease_owner=owner,
            ),
        )
        if first.morelogin_env_id == second.morelogin_env_id:
            raise RuntimeError("PostgreSQL 双 claim 得到相同 env_id")
        try:
            await pool.claim(
                task_id=str(uuid4()),
                session_id=f"pg-live-3-{uuid4().hex[:8]}",
                lease_seconds=120,
                lease_owner=owner,
            )
            third_code = "UNEXPECTED_CLAIM"
        except DevicePoolError as exc:
            third_code = exc.error_code
        if third_code == "UNEXPECTED_CLAIM":
            raise RuntimeError("PostgreSQL 池满第三人不应领取到租约")
        return {
            "seeded": seeded,
            "mutex_env_ids": sorted({first.morelogin_env_id, second.morelogin_env_id}),
            "third_error_code": third_code,
        }
    finally:
        if first is not None:
            await pool.release(lease=first)
        if second is not None:
            await pool.release(lease=second)


async def main() -> int:
    slots, env_ids = _require_live_config()
    out_dir = ROOT / ".alioth-mobile-demo" / "dual-line-smoke"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"slots={slots}")
    print(f"env_ids={env_ids}")
    print("preflight: Open API 拉两台 ADB 端点…")
    preflight = _preflight_adb_endpoints(env_ids)
    for row in preflight:
        print(
            f"  env={row['env_id']} name={row.get('env_name') or '-'} "
            f"endpoint={row['endpoint']}"
        )
        if row.get("blocked"):
            raise RuntimeError(f"{row['env_id']}：{row['blocked']}")
    print("powerOn/ADB 刷新会计费；两条线顺序实连（同机并行 adb 易互踩）")

    line_a = await asyncio.to_thread(_bring_up_line, slots[0], env_ids[0], out_dir)
    line_b = await asyncio.to_thread(_bring_up_line, slots[1], env_ids[1], out_dir)
    if line_a.endpoint == line_b.endpoint:
        raise RuntimeError(f"两条线 ADB 端点相同：{line_a.endpoint}")
    if line_a.env_id == line_b.env_id:
        raise RuntimeError("两条线 env_id 相同")
    if line_a.android_id and line_b.android_id and line_a.android_id == line_b.android_id:
        raise RuntimeError(f"两条线 android_id 相同：{line_a.android_id}")
    if line_a.screenshot_sha256 == line_b.screenshot_sha256:
        print("WARN: 两张截图 SHA-256 相同，仍以 endpoint/android_id 区分为准")

    print("isolation: 只再拍 B 线，确认另一台仍可用")
    _bring_up_line(slots[1], env_ids[1], out_dir)

    pool_report = await _smoke_in_process_pool(slots, env_ids)
    postgres_report: dict[str, object] | None
    try:
        postgres_report = await _smoke_postgres_pool()
    except Exception as exc:  # noqa: BLE001 - 实连 ADB 已过，PG 失败单独记录
        postgres_report = {"skipped_or_failed": f"{type(exc).__name__}: {exc}"}

    report = {
        "ok": True,
        "checked_at": datetime.now(UTC).isoformat(),
        "lines": [asdict(line_a), asdict(line_b)],
        "in_process_pool": pool_report,
        "postgres_pool": postgres_report,
    }
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report: {report_path}")
    print("DUAL_LINE_LIVE_SMOKE_OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR {type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc
