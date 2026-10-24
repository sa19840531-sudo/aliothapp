"""MoreLogin 云机批量：开机、开 ADB、Docker 内验收四步。结果写入 JSON。"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PROFILES: list[tuple[str, str, str]] = [
    ("qa-a", "CP-1", "1716966721435348"),
    ("qa-b", "CP-2", "1721139888752705"),
    ("qa-c", "CP-3", "1721455845687143"),
    ("qa-d", "CP-4", "1721456307046723"),
    ("qa-e", "CP-5", "1721456307046724"),
    ("qa-f", "CP-6", "1721456307046725"),
    ("qa-g", "CP-7", "1721456307046726"),
    ("qa-h", "CP-8", "1721456307046727"),
    ("qa-i", "CP-9", "1721456307046728"),
    ("qa-j", "CP-10", "1721456307046729"),
]

OUT = Path(__file__).resolve().parents[1] / ".tmp_morelogin_fleet_acceptance.json"


@dataclass
class AcceptResult:
    profile_id: str
    cp_name: str
    env_id: str
    env_status: int | None = None
    enable_adb: bool | None = None
    endpoint: str | None = None
    endpoint_kind: str = "unknown"
    power_on_logs: list[str] = field(default_factory=list)
    adb_refresh_logs: list[str] = field(default_factory=list)
    adb_connect_ok: bool = False
    boot_completed: str | None = None
    screencap_ok: int = 0
    screencap_fail: int = 0
    screencap_errors: list[str] = field(default_factory=list)
    passed: bool = False
    block_reason: str | None = None


def _endpoint_kind(endpoint: str | None) -> str:
    if not endpoint:
        return "missing"
    host = endpoint.rsplit(":", 1)[0].strip().lower()
    if host in {"127.0.0.1", "localhost", "0.0.0.0", "::1", "host.docker.internal"}:
        return "loopback"
    if host.replace(".", "").isdigit():
        return "public_ip"
    return "other"


def _prepare_env(env_id: str, *, need_power_on: bool) -> tuple[dict | None, list[str]]:
    from services.api.app.infrastructure.device.morelogin_adb import (
        enable_morelogin_adb,
        ensure_morelogin_cloudphone_powered_on,
        fetch_morelogin_phone_status,
        refresh_cloud_phone_adb_from_morelogin,
    )

    logs: list[str] = []
    if need_power_on:
        try:
            logs.extend(
                ensure_morelogin_cloudphone_powered_on(
                    env_id,
                    wait_seconds=180.0,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logs.append(f"power_on_failed: {exc}")
            return None, logs

    try:
        status = fetch_morelogin_phone_status(env_id)
        logs.append(
            f"status envStatus={status.env_status} enableAdb={status.enable_adb}"
        )
        if not status.enable_adb:
            enable_morelogin_adb(env_id)
            logs.append("updateAdb enableAdb=true")
            time.sleep(2.0)
    except Exception as exc:  # noqa: BLE001
        logs.append(f"status_or_enable_failed: {exc}")
        return None, logs

    try:
        info, refresh_logs = refresh_cloud_phone_adb_from_morelogin(
            env_id,
            apply_to_process_env=False,
        )
        logs.extend(refresh_logs)
        if info is None:
            return None, logs
        return {
            "env_status": info.env_status,
            "enable_adb": info.enable_adb,
            "endpoint": info.endpoint,
        }, logs
    except Exception as exc:  # noqa: BLE001
        logs.append(f"refresh_failed: {exc}")
        return None, logs


def _acceptance_probe(env_id: str, endpoint: str) -> tuple[bool, str | None, int, int, list[str]]:
    from services.api.app.infrastructure.device.adb_ops import (
        BACKEND_CLOUD_PHONE,
        open_adb_device_ops,
    )
    from services.api.app.infrastructure.device.cloud_phone import CloudPhoneController

    errors: list[str] = []
    try:
        ctl = CloudPhoneController(
            morelogin_env_id=env_id,
            allow_global_morelogin_fallback=False,
            apply_morelogin_to_process_env=False,
        )
        serial, _logs = ctl.ensure_ready(timeout_seconds=60)
        ops = open_adb_device_ops(
            BACKEND_CLOUD_PHONE,
            log=lambda _m: None,
            cloud_phone_controller=ctl,
        )
        boot = ""
        try:
            boot_resp = ctl._adb(  # noqa: SLF001
                ["-s", serial, "shell", "getprop", "sys.boot_completed"],
                timeout_seconds=20.0,
            )
            boot = "\n".join(
                p for p in (boot_resp.stdout, boot_resp.stderr) if p
            ).strip()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"boot: {type(exc).__name__}: {exc}")
        ok = fail = 0
        for i in range(10):
            with tempfile.TemporaryDirectory() as td:
                dest = Path(td) / f"cap_{i}.png"
                try:
                    ops.screencap(dest)
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    fail += 1
                    errors.append(f"screencap_{i+1}: {type(exc).__name__}: {exc}")
        return True, boot, ok, fail, errors
    except Exception as exc:  # noqa: BLE001
        errors.append(f"connect: {type(exc).__name__}: {exc}")
        return False, None, 0, 0, errors


def main() -> int:
    only = {p.strip() for p in sys.argv[1:] if p.strip()}
    results: list[AcceptResult] = []
    for profile_id, cp_name, env_id in PROFILES:
        if only and profile_id not in only and env_id not in only:
            continue
        row = AcceptResult(profile_id=profile_id, cp_name=cp_name, env_id=env_id)
        need_power = profile_id in {"qa-f", "qa-g", "qa-h", "qa-i", "qa-j"}
        meta, prep_logs = _prepare_env(env_id, need_power_on=need_power)
        row.power_on_logs = prep_logs
        if meta is None:
            row.block_reason = "prepare_failed"
            results.append(row)
            print(f"{profile_id}: PREP FAIL")
            continue
        row.env_status = meta.get("env_status")
        row.enable_adb = meta.get("enable_adb")
        row.endpoint = meta.get("endpoint")
        row.endpoint_kind = _endpoint_kind(row.endpoint)
        if row.endpoint_kind == "loopback":
            row.block_reason = "loopback_adb_not_public"
            results.append(row)
            print(f"{profile_id}: LOOPBACK {row.endpoint}")
            continue
        if row.endpoint_kind != "public_ip":
            row.block_reason = f"endpoint_kind={row.endpoint_kind}"
            results.append(row)
            print(f"{profile_id}: BAD ENDPOINT {row.endpoint}")
            continue
        connect_ok, boot, ok, fail, errs = _acceptance_probe(env_id, row.endpoint or "")
        row.adb_connect_ok = connect_ok
        row.boot_completed = boot
        row.screencap_ok = ok
        row.screencap_fail = fail
        row.screencap_errors = errs[:5]
        row.passed = connect_ok and boot == "1" and ok == 10 and fail == 0
        if not row.passed:
            row.block_reason = "acceptance_failed"
        results.append(row)
        status = "PASS" if row.passed else "FAIL"
        print(f"{profile_id}: {status} endpoint={row.endpoint} screencap={ok}/10 boot={boot}")

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "passed_profiles": [r.profile_id for r in results if r.passed],
        "results": [asdict(r) for r in results],
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {OUT}")
    print("passed:", payload["passed_profiles"])
    return 0 if payload["passed_profiles"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
