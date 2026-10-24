"""MoreLogin：开跑前唤醒云手机并拉取最新 ADB。

MoreLogin 关机再开后 adbIp/adbPort/连线码会变，不可写死在 .env。
默认优先官方 Open API（``https://api.morelogin.com`` + OAuth2），服务器无需安装客户端。
未配置 API ID/Key 时回退本机 Local API ``http://127.0.0.1:40000``。

环境变量：
- ``ALIOTH_MORELOGIN_API_MODE``：auto／open／local
- ``ALIOTH_MORELOGIN_API_ID``／``ALIOTH_MORELOGIN_API_KEY``：Open API 凭证
- ``ALIOTH_MORELOGIN_CLOUDPHONE_ID``：单机回退用云手机 envId；多机路径必须传租约 env_id
- ``ALIOTH_MORELOGIN_LOCAL_API``：Local 模式基址
- ``ALIOTH_MORELOGIN_OPEN_API``：Open 模式基址
- ``ALIOTH_MORELOGIN_ADB_AUTO_REFRESH``：默认开
- ``ALIOTH_MORELOGIN_ADB_AUTO_ENABLE``：若尚未 enableAdb，默认会呼叫 updateAdb
- ``ALIOTH_MORELOGIN_ADB_AUTO_POWER_ON``：关机时是否 powerOn（**默认关**，需显式设 1）
- ``ALIOTH_MORELOGIN_ADB_AUTO_POWER_OFF``：质检释放／隔离租约后是否 powerOff（**默认开**，省计费；
  测试频繁开关机可设 ``0``。开启时若仍有排队／待领设备的 App 任务会跳过关机以连跑）
- ``ALIOTH_MORELOGIN_POWER_ON_WAIT_SECONDS``：等待开机就绪秒数，默认 180
- ``ALIOTH_CLOUD_PHONE_ADB_LOOPBACK_HOST``：把 MoreLogin 返回的 ``localhost``／``127.0.0.1``
  ADB 改写到该主机（Docker 内默认 ``host.docker.internal``，本机进程不改写）
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from services.api.app.infrastructure.device.morelogin_http import (
    MoreLoginAdbError,
    morelogin_cloudphone_url,
    morelogin_transport_kind,
    post_morelogin_json,
    resolve_morelogin_api_base,
    resolve_morelogin_local_api_base,
)

__all__ = [
    "MoreLoginAdbError",
    "MoreLoginAdbInfo",
    "MoreLoginPhoneStatus",
    "apply_morelogin_adb_to_env",
    "enable_morelogin_adb",
    "ensure_morelogin_cloudphone_powered_on",
    "fetch_morelogin_adb_info",
    "fetch_morelogin_phone_status",
    "morelogin_adb_auto_enable_enabled",
    "morelogin_adb_auto_power_off_enabled",
    "morelogin_adb_auto_power_on_enabled",
    "morelogin_adb_auto_refresh_enabled",
    "power_off_morelogin_cloudphone",
    "power_on_morelogin_cloudphone",
    "maybe_power_off_morelogin_after_lease",
    "maybe_power_off_morelogin_fleet_idle",
    "list_pool_morelogin_env_ids",
    "refresh_cloud_phone_adb_from_morelogin",
    "resolve_cloud_phone_adb_loopback_host",
    "resolve_morelogin_cloudphone_id",
    "resolve_morelogin_local_api_base",
    "resolve_morelogin_power_on_wait_seconds",
    "rewrite_loopback_adb_endpoint",
]


def _post_json(url: str, payload: dict[str, Any], *, timeout_seconds: float = 20.0) -> dict[str, Any]:
    """供单测 monkeypatch；生产走 Open/Local HTTP。"""
    return post_morelogin_json(url, payload, timeout_seconds=timeout_seconds)


# 只有 4 表示已开机；3 为开机中，5/6/7 为重置、重启或换机中。
_READY_ENV_STATUS = 4
_POWER_OPERATION_IN_PROGRESS_STATUSES = frozenset({3, 5, 6, 7})
# 仅这两个官方错误码证明已开始开机或已经运行；不能按英文片段猜测。
_POWER_ON_STARTED_CODES = frozenset({33323, 33324})


@dataclass(frozen=True)
class MoreLoginAdbInfo:
    """單台雲手機的 ADB 連線資訊。"""

    env_id: str
    endpoint: str
    auth: str
    enable_adb: bool
    env_status: int | None = None
    env_name: str | None = None


@dataclass(frozen=True)
class MoreLoginPhoneStatus:
    """雲手機開機狀態（可不含 ADB）。"""

    env_id: str
    env_status: int | None
    enable_adb: bool
    env_name: str | None = None
    adb: MoreLoginAdbInfo | None = None


def morelogin_adb_auto_refresh_enabled() -> bool:
    raw = (os.getenv("ALIOTH_MORELOGIN_ADB_AUTO_REFRESH") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def morelogin_adb_auto_enable_enabled() -> bool:
    raw = (os.getenv("ALIOTH_MORELOGIN_ADB_AUTO_ENABLE") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def morelogin_adb_auto_power_on_enabled() -> bool:
    # 預設關閉：開機會計費，必須顯式 opt-in
    raw = (os.getenv("ALIOTH_MORELOGIN_ADB_AUTO_POWER_ON") or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def morelogin_adb_auto_power_off_enabled() -> bool:
    """质检结束后是否自动关机；默认开，避免云手机空转计费。"""
    raw = (os.getenv("ALIOTH_MORELOGIN_ADB_AUTO_POWER_OFF") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def resolve_morelogin_power_on_wait_seconds() -> float:
    raw = (os.getenv("ALIOTH_MORELOGIN_POWER_ON_WAIT_SECONDS") or "180").strip()
    try:
        return max(30.0, float(raw))
    except ValueError:
        return 180.0


def resolve_morelogin_cloudphone_id() -> str:
    return (os.getenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID") or "").strip()


def _env_id_payload(env_id: str) -> int | str:
    eid = str(env_id).strip()
    return int(eid) if eid.isdigit() else eid


def _page_request_body(page_no: int, page_size: int, *, api_base: str) -> dict[str, Any]:
    if morelogin_transport_kind(api_base) == "open":
        return {"pageNo": page_no, "pageSize": page_size}
    return {"page": page_no, "pageSize": page_size}


def _resolved_api_base(api_base: str | None) -> str:
    return (api_base or resolve_morelogin_api_base()).rstrip("/")


def _as_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any, *, default: bool = False) -> bool:
    """嚴格解析供應商布林；字串 \"false\"／\"0\" 不得當成 True。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in ("1", "true", "yes", "on", "y"):
            return True
        if raw in ("0", "false", "no", "off", "n", ""):
            return False
        return default
    return bool(value)


def resolve_cloud_phone_adb_loopback_host() -> str:
    """解析 loopback ADB 改写目标；空字串表示不改写。"""
    configured = (os.getenv("ALIOTH_CLOUD_PHONE_ADB_LOOPBACK_HOST") or "").strip()
    if configured:
        return configured
    # Docker 容器内 localhost 指向容器自身，须改到宿主机网关。
    if Path("/.dockerenv").exists():
        return "host.docker.internal"
    return ""


def rewrite_loopback_adb_endpoint(endpoint: str) -> str:
    """将 localhost／127.0.0.1 ADB 端点改写到可从当前进程到达的主机。

    跳板隧道模式保留 ``127.0.0.1``：SSH -R 只绑跳板 localhost，Docker
    ``host.docker.internal`` 改写会连错（Linux 上常达不成 127.0.0.1 监听）。
    """
    raw = str(endpoint or "").strip()
    if not raw or ":" not in raw:
        return raw
    from services.api.app.infrastructure.device.onirent_adb_tunnel import (
        onirent_adb_tunnel_enabled,
        resolve_onirent_adb_tunnel_local_endpoint,
    )

    if onirent_adb_tunnel_enabled():
        # 公网 20615 若误入此路径，一并映射到跳板本机埠
        from services.api.app.infrastructure.device.onirent_adb_tunnel import (
            map_onirent_public_adb_to_tunnel,
        )

        mapped = map_onirent_public_adb_to_tunnel(raw)
        local = resolve_onirent_adb_tunnel_local_endpoint()
        if mapped == local or raw == local:
            return local
        host, sep, _port = mapped.rpartition(":")
        if sep and host.strip().lower() in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
            return mapped
        return mapped

    host, sep, port = raw.rpartition(":")
    if not sep or not port:
        return raw
    if host.strip().lower() not in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return raw
    target = resolve_cloud_phone_adb_loopback_host()
    if not target:
        return raw
    return f"{target}:{port.strip()}"


def _parse_adb_row(row: dict[str, Any], *, fallback_id: str) -> MoreLoginAdbInfo | None:
    """從 page/dataList 或 batchAdbInfo 單列解析 ADB。"""
    env_id = str(row.get("id") or fallback_id).strip()
    adb_block = row.get("adbInfo") if isinstance(row.get("adbInfo"), dict) else None
    adb_ip = str((adb_block or row).get("adbIp") or "").strip()
    adb_port = str((adb_block or row).get("adbPort") or "").strip()
    auth = str((adb_block or row).get("adbPassword") or "").strip()
    enable = row.get("enableAdb")
    if enable is None and adb_block is not None:
        enable = True
    env_status = _as_int(row.get("envStatus"))
    if not adb_ip or not adb_port:
        return None
    return MoreLoginAdbInfo(
        env_id=env_id,
        endpoint=rewrite_loopback_adb_endpoint(f"{adb_ip}:{adb_port}"),
        auth=auth,
        enable_adb=_as_bool(enable, default=False),
        env_status=env_status,
        env_name=str(row.get("envName") or "") or None,
    )


def _parse_phone_status(row: dict[str, Any], *, fallback_id: str) -> MoreLoginPhoneStatus:
    env_id = str(row.get("id") or fallback_id).strip()
    return MoreLoginPhoneStatus(
        env_id=env_id,
        env_status=_as_int(row.get("envStatus")),
        enable_adb=_as_bool(row.get("enableAdb"), default=False),
        env_name=str(row.get("envName") or "") or None,
        adb=_parse_adb_row(row, fallback_id=env_id),
    )


def _iter_morelogin_page_rows(
    *,
    api_base: str,
    timeout_seconds: float = 20.0,
    page_size: int = 50,
    max_pages: int = 40,
) -> list[dict[str, Any]]:
    """分頁拉取 cloudphone/page 全部列（避免只掃第一頁 50 條漏目標）。"""
    base = api_base.rstrip("/")
    rows: list[dict[str, Any]] = []
    page_no = 1
    while page_no <= max_pages:
        page = _post_json(
            morelogin_cloudphone_url("page", api_base=base),
            _page_request_body(page_no, page_size, api_base=base),
            timeout_seconds=timeout_seconds,
        )
        if int(page.get("code") or 0) != 0:
            raise MoreLoginAdbError(
                f"MoreLogin page 失敗：code={page.get('code')} msg={page.get('msg')} page={page_no}"
            )
        data = page.get("data") or {}
        data_list = data.get("dataList") if isinstance(data, dict) else None
        if not isinstance(data_list, list) or not data_list:
            break
        for row in data_list:
            if isinstance(row, dict):
                rows.append(row)
        total = _as_int(data.get("total") if isinstance(data, dict) else None)
        if total is not None and len(rows) >= total:
            break
        if len(data_list) < page_size:
            break
        page_no += 1
    return rows


def fetch_morelogin_phone_status(
    env_id: str,
    *,
    api_base: str | None = None,
    timeout_seconds: float = 20.0,
) -> MoreLoginPhoneStatus:
    """查詢雲手機開機狀態（分頁掃描）；找不到則拋錯。"""
    base = _resolved_api_base(api_base)
    eid = str(env_id).strip()
    if not eid:
        raise MoreLoginAdbError("ALIOTH_MORELOGIN_CLOUDPHONE_ID 未設定")

    for row in _iter_morelogin_page_rows(api_base=base, timeout_seconds=timeout_seconds):
        if str(row.get("id") or "") != eid:
            continue
        return _parse_phone_status(row, fallback_id=eid)
    raise MoreLoginAdbError(f"MoreLogin 列表找不到雲手機 id={eid}")


def fetch_morelogin_adb_info(
    env_id: str,
    *,
    api_base: str | None = None,
    timeout_seconds: float = 20.0,
) -> MoreLoginAdbInfo:
    """查詢指定雲手機目前 ADB；優先 batchAdbInfo，失敗再掃 page。"""
    base = _resolved_api_base(api_base)
    eid = str(env_id).strip()
    if not eid:
        raise MoreLoginAdbError("ALIOTH_MORELOGIN_CLOUDPHONE_ID 未設定")

    # 1) batchAdbInfo
    batch = _post_json(
        morelogin_cloudphone_url("batchAdbInfo", api_base=base),
        {"envIds": [_env_id_payload(eid)]},
        timeout_seconds=timeout_seconds,
    )
    if int(batch.get("code") or 0) == 0:
        rows = batch.get("data") or []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                rid = str(row.get("id") or "")
                if rid and rid != eid:
                    continue
                info = _parse_adb_row(row, fallback_id=eid)
                if info:
                    return info

    # 2) page 列表內嵌 adbInfo
    status = fetch_morelogin_phone_status(eid, api_base=base, timeout_seconds=timeout_seconds)
    if status.adb:
        return status.adb
    raise MoreLoginAdbError(
        f"雲手機 {eid} 尚無 adbIp/adbPort（enableAdb={status.enable_adb}, "
        f"envStatus={status.env_status}）；請確認已開機且已開啟 ADB"
    )


def enable_morelogin_adb(
    env_id: str,
    *,
    api_base: str | None = None,
    timeout_seconds: float = 20.0,
) -> None:
    """呼叫 updateAdb 開啟指定雲手機 ADB。"""
    base = _resolved_api_base(api_base)
    eid = str(env_id).strip()
    payload = {
        "enableAdb": True,
        "ids": [_env_id_payload(eid)],
    }
    resp = _post_json(
        morelogin_cloudphone_url("updateAdb", api_base=base),
        payload,
        timeout_seconds=timeout_seconds,
    )
    if int(resp.get("code") or 0) != 0:
        raise MoreLoginAdbError(
            f"MoreLogin updateAdb 失敗：code={resp.get('code')} msg={resp.get('msg')}"
        )


def power_on_morelogin_cloudphone(
    env_id: str,
    *,
    api_base: str | None = None,
    headless: bool = True,
    timeout_seconds: float = 60.0,
) -> None:
    """呼叫 powerOn 喚醒雲手機（會計費／扣時長）。

    body 為單個 ``id``（非整列 ids）；已開機時 API 可能回非 0，由呼叫端決定是否忽略。
    """
    base = _resolved_api_base(api_base)
    eid = str(env_id).strip()
    payload: dict[str, Any] = {"id": _env_id_payload(eid)}
    if morelogin_transport_kind(base) == "open":
        payload["disableMoneySavingMode"] = True
    else:
        payload["headless"] = bool(headless)
    resp = _post_json(
        morelogin_cloudphone_url("powerOn", api_base=base),
        payload,
        timeout_seconds=timeout_seconds,
    )
    code = int(resp.get("code") or 0)
    if code != 0:
        msg = str(resp.get("msg") or "")
        # 33323 为开机中，33324 为已运行；其余业务拒绝保留原始原因。
        if code in _POWER_ON_STARTED_CODES:
            return
        request_id = str(resp.get("requestId") or "").strip()
        request_context = f" requestId={request_id}" if request_id else ""
        raise MoreLoginAdbError(
            f"MoreLogin powerOn 失敗：code={code} msg={msg}{request_context}"
        )


def power_off_morelogin_cloudphone(
    env_id: str,
    *,
    api_base: str | None = None,
    timeout_seconds: float = 60.0,
) -> None:
    """呼叫 powerOff 關閉雲手機，避免質檢結束後空轉計費。

    body 為單個 ``id``；已關機／關機中視為成功（軟錯誤）。
    """
    base = _resolved_api_base(api_base)
    eid = str(env_id).strip()
    if not eid:
        raise MoreLoginAdbError("MoreLogin powerOff 缺少 envId")
    resp = _post_json(
        morelogin_cloudphone_url("powerOff", api_base=base),
        {"id": _env_id_payload(eid)},
        timeout_seconds=timeout_seconds,
    )
    code = int(resp.get("code") or 0)
    if code != 0:
        msg = str(resp.get("msg") or "")
        soft = (
            ("已关" in msg)
            or ("已關" in msg)
            or ("stop" in msg.lower())
            or ("stopped" in msg.lower())
            or ("not running" in msg.lower())
            or ("关机" in msg)
            or ("關機" in msg)
        )
        if soft:
            return
        raise MoreLoginAdbError(f"MoreLogin powerOff 失敗：code={code} msg={msg}")


def maybe_power_off_morelogin_after_lease(
    *,
    backend: str,
    morelogin_env_id: str,
    reason: str = "lease_settled",
) -> list[str]:
    """租約釋放／隔離後盡力關機；失敗只記日誌字串，不拋出。"""
    logs: list[str] = []
    if str(backend or "").strip() != "cloud_phone":
        return logs
    from services.api.app.infrastructure.device.cloud_phone_vendor import (
        cloud_phone_vendor_is_onirent,
        looks_like_adb_endpoint,
    )

    if cloud_phone_vendor_is_onirent() or looks_like_adb_endpoint(morelogin_env_id):
        logs.append("onirent auto power-off skipped (static ADB; no powerOff API)")
        return logs
    if not morelogin_adb_auto_power_off_enabled():
        logs.append("morelogin auto power-off disabled")
        return logs
    eid = str(morelogin_env_id or "").strip()
    if not eid:
        logs.append("morelogin auto power-off skipped (env_id missing)")
        return logs
    try:
        power_off_morelogin_cloudphone(eid)
        logs.append(f"morelogin powerOff envId={eid} reason={reason}")
    except Exception as exc:  # noqa: BLE001 - 关机失败不得回滚租约结算
        logs.append(f"morelogin powerOff note: {type(exc).__name__}: {exc}")
    return logs


def list_pool_morelogin_env_ids() -> list[str]:
    """读取库存映射中的全部云手机 envId／ONIRENT ADB（qa-a～qa-j 等）。"""
    from services.api.app.infrastructure.device.cloud_phone_vendor import (
        cloud_phone_vendor_is_onirent,
        list_pool_onirent_adb_endpoints,
    )

    if cloud_phone_vendor_is_onirent():
        return list_pool_onirent_adb_endpoints()
    ids: list[str] = []
    seen: set[str] = set()
    for raw in (os.getenv("ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS") or "").split(","):
        eid = raw.strip()
        if not eid or eid in seen:
            continue
        seen.add(eid)
        ids.append(eid)
    global_id = resolve_morelogin_cloudphone_id()
    if global_id and global_id not in seen:
        ids.append(global_id)
    return ids


def maybe_power_off_morelogin_fleet_idle(
    *,
    skip_env_ids: set[str] | frozenset[str] | None = None,
    reason: str = "fleet_idle",
) -> list[str]:
    """关闭库存内全部空闲云手机；仍有活跃租约的 envId 跳过，避免误杀并发任务。"""
    logs: list[str] = []
    from services.api.app.infrastructure.device.cloud_phone_vendor import (
        cloud_phone_vendor_is_onirent,
    )

    if cloud_phone_vendor_is_onirent():
        logs.append("onirent fleet auto power-off skipped (static ADB; no powerOff API)")
        return logs
    if not morelogin_adb_auto_power_off_enabled():
        logs.append("morelogin fleet auto power-off disabled")
        return logs
    skip = {str(x).strip() for x in (skip_env_ids or set()) if str(x).strip()}
    targets = [eid for eid in list_pool_morelogin_env_ids() if eid not in skip]
    if not targets:
        logs.append("morelogin fleet power-off skipped (no idle env_id)")
        return logs
    logs.append(
        f"morelogin fleet power-off start count={len(targets)} "
        f"skip={len(skip)} reason={reason}"
    )
    for eid in targets:
        try:
            power_off_morelogin_cloudphone(eid)
            logs.append(f"morelogin powerOff envId={eid} reason={reason}")
        except Exception as exc:  # noqa: BLE001 - 单台失败不阻断整池
            logs.append(
                f"morelogin powerOff note envId={eid}: {type(exc).__name__}: {exc}"
            )
    return logs


def ensure_morelogin_cloudphone_powered_on(
    env_id: str,
    *,
    api_base: str | None = None,
    wait_seconds: float | None = None,
) -> list[str]:
    """未开机时请求开机，已有电源操作则只等待，直到状态精确为 4。"""
    logs: list[str] = []
    if not morelogin_adb_auto_power_on_enabled():
        logs.append("morelogin auto power-on disabled")
        return logs

    base = _resolved_api_base(api_base)
    eid = str(env_id).strip()
    status = fetch_morelogin_phone_status(eid, api_base=base)
    logs.append(
        f"morelogin phone status name={status.env_name or '-'} "
        f"envStatus={status.env_status} enableAdb={status.enable_adb}"
    )
    if status.env_status == _READY_ENV_STATUS:
        logs.append("morelogin phone already powered on")
        return logs

    if status.env_status in _POWER_OPERATION_IN_PROGRESS_STATUSES:
        logs.append(f"morelogin existing power operation envStatus={status.env_status}")
    else:
        logs.append(f"morelogin powerOn envId={eid} (was envStatus={status.env_status})")
        power_on_morelogin_cloudphone(eid, api_base=base)
        logs.append("morelogin powerOn requested")

    deadline = time.time() + (wait_seconds if wait_seconds is not None else resolve_morelogin_power_on_wait_seconds())
    last_status: int | None = status.env_status
    last_log = 0.0
    while time.time() < deadline:
        cur = fetch_morelogin_phone_status(eid, api_base=base)
        last_status = cur.env_status
        if cur.env_status == _READY_ENV_STATUS:
            logs.append(f"morelogin phone ready envStatus={cur.env_status}")
            return logs
        now = time.time()
        if now - last_log >= 10.0:
            logs.append(f"waiting morelogin power-on… envStatus={cur.env_status}")
            last_log = now
        time.sleep(3.0)

    raise MoreLoginAdbError(
        f"等待雲手機開機逾時（最後 envStatus={last_status}，需 ={_READY_ENV_STATUS}）；"
        "請確認已綁 SOCKS5 代理，且 Open API 憑證有效或 Local API 客戶端在線"
    )


def apply_morelogin_adb_to_env(info: MoreLoginAdbInfo) -> None:
    """把最新 ADB 寫入進程環境，供 CloudPhoneController / adb connect 使用。"""
    endpoint = rewrite_loopback_adb_endpoint(info.endpoint)
    os.environ["ALIOTH_CLOUD_PHONE_ADB"] = endpoint
    os.environ["ALIOTH_REDROID_ADB"] = endpoint
    os.environ["ALIOTH_ADB_SERIAL"] = endpoint
    if info.auth:
        os.environ["ALIOTH_CLOUD_PHONE_ADB_AUTH"] = info.auth
    else:
        # 新回應無連線碼時清除舊值，避免帶到錯誤裝置
        os.environ.pop("ALIOTH_CLOUD_PHONE_ADB_AUTH", None)


def refresh_cloud_phone_adb_from_morelogin(
    env_id: str | None = None,
    *,
    allow_global_fallback: bool = True,
    apply_to_process_env: bool = True,
) -> tuple[MoreLoginAdbInfo | None, list[str]]:
    """開跑前：必要時喚醒 → 開 ADB → 拉最新端點。

    多機路徑必須顯式傳入租約 ``env_id``；全域 ``ALIOTH_MORELOGIN_CLOUDPHONE_ID``
    僅在 ``allow_global_fallback=True`` 時作為單機回退。

    ONIRENT／靜態 ``host:port``：直接套用端點，不呼叫 MoreLogin。

    回傳 ``(info|None, logs)``。連線碼不寫入日誌全文。
    """
    logs: list[str] = []
    from services.api.app.infrastructure.device.cloud_phone_vendor import (
        cloud_phone_vendor_is_onirent,
        looks_like_adb_endpoint,
    )

    resolved_env_id = str(env_id or "").strip()
    if not resolved_env_id:
        if not allow_global_fallback:
            raise MoreLoginAdbError(
                "多机路径禁止回退全域 ALIOTH_MORELOGIN_CLOUDPHONE_ID"
            )
        if cloud_phone_vendor_is_onirent():
            from services.api.app.infrastructure.device.cloud_phone_vendor import (
                list_pool_onirent_adb_endpoints,
            )

            pool = list_pool_onirent_adb_endpoints()
            resolved_env_id = pool[0] if pool else ""
            if not resolved_env_id:
                resolved_env_id = (os.getenv("ALIOTH_CLOUD_PHONE_ADB") or "").strip()
        else:
            resolved_env_id = resolve_morelogin_cloudphone_id()

    # ONIRENT：绑定值本身就是固定 ADB 端点
    if resolved_env_id and (
        cloud_phone_vendor_is_onirent() or looks_like_adb_endpoint(resolved_env_id)
    ):
        endpoint = rewrite_loopback_adb_endpoint(resolved_env_id)
        info = MoreLoginAdbInfo(
            env_id=resolved_env_id,
            endpoint=endpoint,
            auth="",
            enable_adb=True,
            env_status=_READY_ENV_STATUS,
            env_name="onirent",
        )
        if apply_to_process_env:
            apply_morelogin_adb_to_env(info)
        logs.append(
            f"onirent static adb applied endpoint={info.endpoint} "
            f"(no MoreLogin API / power cycle)"
        )
        return info, logs

    if not morelogin_adb_auto_refresh_enabled():
        logs.append("morelogin adb auto-refresh disabled")
        return None, logs

    if not resolved_env_id:
        logs.append("morelogin adb auto-refresh skipped (ALIOTH_MORELOGIN_CLOUDPHONE_ID unset)")
        return None, logs

    api_base = resolve_morelogin_api_base()
    logs.append(
        f"morelogin adb refresh via {api_base} "
        f"mode={morelogin_transport_kind(api_base)} envId={resolved_env_id}"
    )

    # 1) 關機／未就緒 → 自動 powerOn
    logs.extend(ensure_morelogin_cloudphone_powered_on(resolved_env_id, api_base=api_base))

    # 2) 拉 ADB；失敗則嘗試 enableAdb 再查
    try:
        info = fetch_morelogin_adb_info(resolved_env_id, api_base=api_base)
    except MoreLoginAdbError as exc:
        if morelogin_adb_auto_enable_enabled():
            logs.append(f"morelogin adb query miss: {exc}; try updateAdb enable")
            enable_morelogin_adb(resolved_env_id, api_base=api_base)
            time.sleep(1.5)
            info = fetch_morelogin_adb_info(resolved_env_id, api_base=api_base)
        else:
            raise

    if not info.enable_adb and morelogin_adb_auto_enable_enabled():
        logs.append("morelogin enableAdb=false; calling updateAdb")
        enable_morelogin_adb(resolved_env_id, api_base=api_base)
        time.sleep(1.5)
        info = fetch_morelogin_adb_info(resolved_env_id, api_base=api_base)

    if info.env_status != _READY_ENV_STATUS:
        raise MoreLoginAdbError(
            f"雲手機尚未就緒 envStatus={info.env_status}（需 ={_READY_ENV_STATUS} 開機完成）"
        )

    rewritten = rewrite_loopback_adb_endpoint(info.endpoint)
    if rewritten != info.endpoint:
        logs.append(f"morelogin adb loopback rewrite {info.endpoint} -> {rewritten}")
        info = replace(info, endpoint=rewritten)

    if apply_to_process_env:
        apply_morelogin_adb_to_env(info)
    logs.append(
        f"morelogin adb refreshed endpoint={info.endpoint} "
        f"auth={'set' if info.auth else 'unset'} "
        f"name={info.env_name or '-'}"
    )
    return info, logs
