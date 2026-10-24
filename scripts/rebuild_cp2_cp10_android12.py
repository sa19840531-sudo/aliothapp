"""重建 CP-2～CP-10 为 sku 10002（Android 12）并更新 env 映射。"""

from __future__ import annotations

import json
import re
import socket
import time
from pathlib import Path

ROOT = Path(r"F:\aliothapp")
OUT = ROOT / ".tmp_rebuild_cp2_10_result.json"
SKU_ANDROID_12 = "10002"

# 旧机：名称、envId、proxyId（保留原代理绑定）
OLD_FLEET = [
    ("CP-2", 1721139888752705, 1721144774985341),
    ("CP-3", 1721455845687143, 1721456042888240),
    ("CP-4", 1721456307046723, 1721456042888241),
    ("CP-5", 1721456307046724, 1721456042888242),
    ("CP-6", 1721456307046725, 1721456042888243),
    ("CP-7", 1721456307046726, 1721456042888244),
    ("CP-8", 1721456307046727, 1721456042888245),
    ("CP-9", 1721456307046728, 1721139762767093),
    ("CP-10", 1721456307046729, 1721456042888239),
]
CP1_ID = 1716966721435348

ENV_FILES = [
    ROOT / ".env.local",
    ROOT / "infra" / "env" / ".env.local",
    ROOT / ".env.example",
    ROOT / "infra" / "env" / ".env.local.example",
    ROOT / "infra" / "env" / ".env.production.example",
    ROOT / "infra" / "env" / ".env.cloud-phone.example",
    ROOT / "infra" / "env" / ".env.teammate-cloud-phone.example",
]
ENV_KEY = "ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS"


def load_env() -> None:
    import os

    for line in (ROOT / ".env.local").read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    import os as _os

    _os.environ["ALIOTH_MORELOGIN_API_MODE"] = "open"
    _os.environ["ALIOTH_MORELOGIN_ADB_AUTO_POWER_ON"] = "1"


def post(path: str, payload: dict, *, timeout: float = 90.0) -> dict:
    from services.api.app.infrastructure.device.morelogin_http import (
        morelogin_cloudphone_url,
        post_morelogin_json,
        resolve_morelogin_api_base,
    )

    base = resolve_morelogin_api_base()
    return post_morelogin_json(
        morelogin_cloudphone_url(path, api_base=base),
        payload,
        timeout_seconds=timeout,
    )


def endpoint_kind(ip: str | None) -> str:
    host = str(ip or "").strip().lower()
    if not host:
        return "missing"
    if host in {"localhost", "127.0.0.1"}:
        return "loopback"
    if host.replace(".", "").isdigit():
        return "public"
    return "other"


def tcp_ok(ip: str, port: str) -> bool:
    try:
        with socket.create_connection((ip, int(port)), timeout=3):
            return True
    except OSError:
        return False


def wait_ready(env_id: int, *, timeout: float = 180.0) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = (post("batchAdbInfo", {"envIds": [env_id]}).get("data") or [{}])[0]
        status = row.get("envStatus")
        if isinstance(status, int) and status >= 4:
            return status
        time.sleep(3)
    return None


def power_off(env_id: int) -> dict:
    return post("powerOff", {"id": env_id})


def delete_ids(ids: list[int]) -> dict:
    return post("delete/batch", {"ids": ids})


def create_one(*, proxy_id: int, remark: str) -> int:
    resp = post(
        "create",
        {
            "skuId": SKU_ANDROID_12,
            "quantity": 1,
            "envRemark": remark,
            "automaticGeo": True,
            "automaticLanguage": True,
            "automaticLocation": True,
            "proxyId": proxy_id,
        },
    )
    if resp.get("code") != 0:
        raise RuntimeError(f"create failed: {resp}")
    data = resp.get("data") or []
    if not data:
        raise RuntimeError(f"create empty data: {resp}")
    return int(data[0])


def rename(env_id: int, env_name: str) -> dict:
    return post("edit/batch", {"id": [env_id], "envName": env_name})


def power_on(env_id: int) -> dict:
    return post("powerOn", {"id": env_id, "disableMoneySavingMode": True})


def enable_adb(env_id: int) -> dict:
    return post("updateAdb", {"enableAdb": True, "ids": [env_id]})


def adb_row(env_id: int) -> dict:
    return (post("batchAdbInfo", {"envIds": [env_id]}).get("data") or [{}])[0]


def update_env_files(new_ids: list[int]) -> None:
    value = ",".join(str(i) for i in [CP1_ID, *new_ids])
    pattern = re.compile(rf"^{re.escape(ENV_KEY)}=.*$", re.MULTILINE)
    for path in ENV_FILES:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        line = f"{ENV_KEY}={value}"
        if pattern.search(text):
            text = pattern.sub(line, text)
        else:
            text = text.rstrip() + "\n" + line + "\n"
        path.write_text(text, encoding="utf-8")
        print("updated", path, flush=True)


def main() -> None:
    load_env()
    report: dict = {"steps": [], "devices": []}

    old_ids = [item[1] for item in OLD_FLEET]
    print("=== powerOff old fleet ===", flush=True)
    for name, env_id, _proxy in OLD_FLEET:
        try:
            resp = power_off(env_id)
            report["steps"].append({"powerOff": name, "resp": resp})
            print(name, "powerOff", resp.get("code"), resp.get("msg"), flush=True)
        except Exception as exc:  # noqa: BLE001
            report["steps"].append({"powerOff": name, "error": str(exc)})
            print(name, "powerOff ERR", exc, flush=True)
    time.sleep(8)

    print("=== delete/batch ===", flush=True)
    del_resp = delete_ids(old_ids)
    report["delete"] = del_resp
    print("delete", json.dumps(del_resp, ensure_ascii=False), flush=True)
    if del_resp.get("code") != 0:
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit(f"delete failed: {del_resp}")
    time.sleep(5)

    new_ids: list[int] = []
    for name, _old_id, proxy_id in OLD_FLEET:
        item: dict = {"name": name, "proxy_id": proxy_id}
        print(f"=== create {name} ===", flush=True)
        try:
            new_id = create_one(proxy_id=proxy_id, remark=f"alioth-rebuild-{name}")
            item["new_id"] = new_id
            rn = rename(new_id, name)
            item["rename"] = rn
            po = power_on(new_id)
            item["powerOn"] = po
            wait_ready(new_id)
            ea = enable_adb(new_id)
            item["updateAdb"] = ea
            time.sleep(3)
            row = adb_row(new_id)
            ip = str(row.get("adbIp") or "").strip()
            port = str(row.get("adbPort") or "").strip()
            kind = endpoint_kind(ip)
            item["adb"] = {
                "adbIp": ip,
                "adbPort": port,
                "kind": kind,
                "envStatus": row.get("envStatus"),
                "skuId": SKU_ANDROID_12,
                "tcp_ok": tcp_ok(ip, port) if ip and port and kind == "public" else False,
            }
            item["success"] = kind == "public" and item["adb"]["tcp_ok"]
            new_ids.append(new_id)
            print(name, "new_id", new_id, item["adb"], flush=True)
        except Exception as exc:  # noqa: BLE001
            item["error"] = f"{type(exc).__name__}: {exc}"
            item["success"] = False
            print(name, "ERROR", item["error"], flush=True)
        report["devices"].append(item)

    report["new_env_ids"] = new_ids
    report["public_ok"] = [d["name"] for d in report["devices"] if d.get("success")]
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved", OUT, flush=True)

    if len(new_ids) == len(OLD_FLEET):
        update_env_files(new_ids)
        print("ENV_IDS", ",".join(str(i) for i in [CP1_ID, *new_ids]), flush=True)
    else:
        print("WARN: partial create, env files NOT updated", flush=True)

    print("public_ok", report["public_ok"], flush=True)


if __name__ == "__main__":
    main()
