# 雲手機經跳板隧道（代碼直連跳板埠；Alioth 不部署在跳板上）

> 更新日期：2026-09-12  
> **Alioth 只 `adb connect 172.104.172.119:3xxxx`**；反向隧道由負責人 Windows 腳本維持。  
> 契約：`onirent_adb_tunnel.py`（`SOURCE=tunnel`）＋ `infra/scripts/start-linode-adb-tunnel.ps1`

## 拓撲

```text
負責人 Windows                     跳板 Linode                 Alioth（任意機器）
 ONIRENT 可達公網 ADB
        │
        └─ ssh -R 0.0.0.0:30615 ──► 0.0.0.0:30615  ◄── adb connect 172.104.172.119:30615
```

- **不是**把 Alioth 裝在跳板上連 `127.0.0.1`  
- **不是**同事 SSH 登跳板  
- **是**代碼／adb 直連跳板公網 IP 的隧道埠

## 槽位代號與端點

| 代號 | ONIRENT 公網 | 跳板（代碼連這個） |
|------|--------------|-------------------|
| qa-a | `51.89.164.208:20615` | `172.104.172.119:30615` |
| qa-b | `51.195.137.27:20660` | `172.104.172.119:30660` |
| qa-c | `198.244.190.180:20907` | `172.104.172.119:30907` |
| qa-d | `51.195.137.27:20033` | `172.104.172.119:30033` |
| qa-e | `51.89.164.208:20613` | `172.104.172.119:30613` |

`ALIOTH_DEVICE_POOL_SLOTS` 與 `ALIOTH_DEVICE_POOL_ONIRENT_ADB` **必須等長同序**。

## 負責人

```powershell
# 开隧道（默认会另开窗口启动「只保留跳板」守护）
powershell -ExecutionPolicy Bypass -File F:\aliothapp\infra\scripts\start-linode-adb-tunnel.ps1

# 若隧道已在跑，只补守护：
powershell -ExecutionPolicy Bypass -File F:\aliothapp\infra\scripts\guard-linode-adb-jump-only.ps1
```

跳板需：`GatewayPorts clientspecified`；`30615` 等埠勿被 iptables DROP。

### 禁止双线抢连

负责人本机 **不要** 再 `adb connect 51.x / 198.x:2xxxx` 公网埠。  
`guard-linode-adb-jump-only.ps1` 会周期扫描 `adb devices`，自动 `disconnect` 非跳板 TCP 端点，并补连 `172.104.172.119:3xxxx`。  
否则易出现五台抖动、ADB `5037`/`10048`、device offline。

**务必关闭本机 ONIRENT 客户端**（或勿让它长挂公网 ADB）：其自带 `ONIRENT\adb.exe` 会占用 5037、反复 `adb connect` 公网，守护脚本即使踢掉也会被抢回，且 `taskkill adb` 常被拒绝。  
隧道只靠 SSH `-R`，负责人机无需再用 ONIRENT GUI 直连那五台。

## Alioth env

見 `infra/env/.env.linode-shared-tunnel.example`：

```env
ALIOTH_ONIRENT_ADB_SOURCE=tunnel
ALIOTH_ONIRENT_ADB_TUNNEL=1
ALIOTH_DEVICE_POOL_SLOTS=cloud_phone:qa-a,cloud_phone:qa-b,cloud_phone:qa-c,cloud_phone:qa-d,cloud_phone:qa-e
ALIOTH_DEVICE_POOL_ONIRENT_ADB=172.104.172.119:30615,172.104.172.119:30660,172.104.172.119:30907,172.104.172.119:30033,172.104.172.119:30613
ALIOTH_CLOUD_PHONE_ADB=172.104.172.119:30615
```

## 安全

隧道埠已對公網監聽，建議後續加 IP 白名單／雲防火牆；勿再發 root 密碼給同事。
