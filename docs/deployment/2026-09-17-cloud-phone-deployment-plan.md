# Alioth 雲手機部署方案

> 日期：2026-09-17  
> 狀態：運維可執行方案（對齊當前 `app-automation`／`dev_2.0` 契約）  
> 範圍：正式 App 質檢裝置面（`ALIOTH_DEVICE_BACKEND=cloud_phone`）  
> 預設供應商：**ONIRENT（CC 智控）**；MoreLogin 僅作歷史旁路，正式部署預設關閉  
> **上伺服器請直接跟：** [`2026-09-17-cloud-phone-server-runbook.md`](2026-09-17-cloud-phone-server-runbook.md)

關聯文檔：

| 文檔 | 用途 |
|------|------|
| [`2026-09-17-cloud-phone-server-runbook.md`](2026-09-17-cloud-phone-server-runbook.md) | **伺服器 `192.46.230.40` 上線步驟** |
| [`docs/design/2026-07-27-cloud-phone-technical-solution.md`](../design/2026-07-27-cloud-phone-technical-solution.md) | 為何用雲手機、協定邊界 |
| [`docs/design/2026-08-17-app-cloud-phone-concurrency-scheduling.md`](../design/2026-08-17-app-cloud-phone-concurrency-scheduling.md) | 設備租約／並發調度 |
| [`docs/異地ADB跳板-接入說明.md`](../異地ADB跳板-接入說明.md) | 異地經 Linode 跳板 |
| [`docs/deployment/2026-07-17-alioth-single-host-deployment-guide.md`](2026-07-17-alioth-single-host-deployment-guide.md) | 單機 Compose 基線 |
| `infra/env/.env.production.example` | 生產 `alioth.env` 雲手機段落 |
| `infra/env/.env.linode-shared-tunnel.example` | 跳板五台樣例 |
| `infra/env/.env.onirent-direct.example` | 公網 ADB 直連樣例 |

---

## 1. 結論（先看這段）

1. **App 質檢主路徑用雲手機，不用本機模擬器當正式裝置面**（Flutter／ARM 業務包在模擬器常無渲染）。  
2. **供應商選 ONIRENT**；平台只認固定／隧道 ADB（或本機 CC 智控 WS 列表），**禁止**正式環境再開 MoreLogin power／enableAdb／exeCommand。  
3. **部署分兩種拓撲，二選一，勿混連**：  
   - **A. 公網直連**：Alioth 所在機可穩定 `adb connect <公網host:2xxxx>`。  
   - **B. 跳板隧道（異地推薦）**：負責人 Windows 掛 `-R` 隧道；Alioth 只連 `172.104.172.119:3xxxx`。  
4. **設備池用 PostgreSQL 租約**（`ALIOTH_APP_DEVICE_LEASE_BACKEND=postgres`）；`SLOTS` 與 `ONIRENT_ADB` **必須等長同序**。  
5. 改 env／compose 後必須 **重建 api＋worker**（compose 覆蓋會蓋掉 `.env.local`），再清庫內舊 MoreLogin 資源。

---

## 2. 總體架構

```text
瀏覽器 / 工作台
        │
        ▼
┌──── Alioth 主站（Docker）──────────────┐
│  alioth-api / alioth-worker            │
│  DEVICE_BACKEND=cloud_phone            │
│  VENDOR=onirent                        │
│  Device Pool（qc_device_resource/lease）│
└────────────────┬───────────────────────┘
                 │ adb connect（租約綁定的 host:port）
                 ▼
     ┌─── 拓撲 A：公網直連 ───┐    ┌─── 拓撲 B：跳板 ───┐
     │ ONIRENT 公網 2xxxx     │    │ Linode 3xxxx       │
     │（僅本機可達時使用）     │    │ ← 負責人 SSH -R    │
     └────────────────────────┘    └────────────────────┘
                 │
                 ▼
            ONIRENT ARM 雲手機
            （裝包／點擊／截圖／UI dump）
```

職責邊界（不可互相替代）：

| 層 | 職責 |
|----|------|
| Bootstrap lease | 誰跑這個 Session |
| Device lease | 哪台雲手機被佔用 |
| Session Command Gate | 同一 Session 內動作串行 |
| ADB／SurfaceDriver | 實際裝包、截圖、點擊 |

Redis 只做喚醒，**不得**當設備庫存貨源。

---

## 3. 拓撲選擇

### 3.1 拓撲 A：公網 ADB 直連

適用：Alioth 與 ONIRENT 公網 ADB 同網可達、延遲與穩定性可接受。

- 樣例：`infra/env/.env.onirent-direct.example`  
- 關鍵：`ALIOTH_ONIRENT_ADB_SOURCE=fixed`（或 `auto` 且填齊 `DEVICE_POOL_ONIRENT_ADB`）  
- **禁止**同時再掛跳板 `3xxxx` 雙連同一台機（易 offline／搶 5037）

### 3.2 拓撲 B：Linode 跳板（異地／共用推薦）

適用：多人／異地共用同一批五台；Alioth 可部署在任意機器。

```text
負責人 Windows                     跳板 Linode                 Alioth
 ONIRENT 可達公網 ADB
        └─ ssh -R 0.0.0.0:3xxxx ──► :3xxxx  ◄── adb connect 跳板:3xxxx
```

- 樣例：`infra/env/.env.linode-shared-tunnel.example`  
- 腳本：`infra/scripts/start-linode-adb-tunnel.ps1`  
- 守護：`infra/scripts/guard-linode-adb-jump-only.ps1`（只保留跳板連線，踢掉公網雙掛）  
- **五台（含 qa-b）一律進隧道**；跳板埠 = `10000 + 公網埠`

| 代號 | 公網（僅隧道源） | 跳板（代碼連這個） |
|------|------------------|-------------------|
| qa-a | `*:20615` | `172.104.172.119:30615` |
| qa-b | `*:20660` | `172.104.172.119:30660` |
| qa-c | `*:20907` | `172.104.172.119:30907` |
| qa-d | `*:20033` | `172.104.172.119:30033` |
| qa-e | `*:20613` | `172.104.172.119:30613` |

跳板要求：`sshd` 開 `GatewayPorts clientspecified`；防火牆放行 `3xxxx`（建議後續加 IP 白名單）。

### 3.3 本機單機 POC（非正式池）

- `ALIOTH_ONIRENT_ADB_SOURCE=auto`＋本機 CC 智控 WS（`ws://127.0.0.1:33332/`，Docker 用 `host.docker.internal`）  
- 樣例：`infra/env/.env.cloud-phone.example`  
- 正式並行質檢仍應切到固定池＋租約，勿長期只靠 WS 單端點。

---

## 4. 前置條件清單

| 項 | 要求 |
|----|------|
| 主站 | Docker Compose 可起 api／worker／postgres／redis；遷移已跑 |
| ADB | 主站容器或宿主持有 `adb`，能連到選定拓撲的 `host:port` |
| ONIRENT | 帳號與雲手機實例已開機；ADB Shell 可見 `host:port` |
| 跳板（若選 B） | 負責人機可 SSH；隧道腳本常駐；勿再開 ONIRENT GUI 長掛公網 ADB |
| 費用 | 雲手機時長＋（若廠商要求）代理／套餐；與 LLM 費用分開估 |
| MoreLogin | 正式部署：**清空** Local／Open API 與 env_id；所有 `MORELOGIN_*_AUTO_*=0` |

---

## 5. 環境變數契約（正式）

以下為正式部署最小集合（值依拓撲替換）。完整樣例見 `infra/env/`。

```env
ALIOTH_DEVICE_BACKEND=cloud_phone
ALIOTH_CLOUD_PHONE_VENDOR=onirent

# 拓撲 B 用 tunnel；拓撲 A 用 fixed／auto
ALIOTH_ONIRENT_ADB_SOURCE=tunnel
ALIOTH_ONIRENT_ADB_TUNNEL=1
ALIOTH_ONIRENT_ADB_TUNNEL_JUMP_HOST=172.104.172.119
ALIOTH_ONIRENT_ADB_TUNNEL_MAP=...公網=跳板,...

ALIOTH_APP_DEVICE_LEASE_BACKEND=postgres
ALIOTH_DEVICE_POOL_SLOTS=cloud_phone:qa-a,cloud_phone:qa-b,cloud_phone:qa-c,cloud_phone:qa-d,cloud_phone:qa-e
ALIOTH_DEVICE_POOL_ONIRENT_ADB=172.104.172.119:30615,172.104.172.119:30660,172.104.172.119:30907,172.104.172.119:30033,172.104.172.119:30613
ALIOTH_CLOUD_PHONE_ADB=172.104.172.119:30615

# MoreLogin 全部關閉
ALIOTH_CLOUD_PHONE_MORELOGIN_API=adb
ALIOTH_MORELOGIN_ADB_AUTO_POWER_ON=0
ALIOTH_MORELOGIN_ADB_AUTO_POWER_OFF=0
ALIOTH_MORELOGIN_ADB_AUTO_REFRESH=0
ALIOTH_MORELOGIN_ADB_AUTO_ENABLE=0
ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS=
ALIOTH_MORELOGIN_CLOUDPHONE_ID=

ALIOTH_DEVICE_WAIT_MAX_SECONDS=600
ALIOTH_DEVICE_LEASE_RECOVERY_INTERVAL_SECONDS=5
ALIOTH_DEVICE_LEASE_RECOVERY_GRACE_SECONDS=60
```

硬規則：

1. `DEVICE_POOL_SLOTS` 與 `DEVICE_POOL_ONIRENT_ADB` **條數相同、順序一致**。  
2. compose 裡若寫死 MoreLogin auto＝1，會覆蓋 `.env` → **改 compose 再 recreate**。  
3. `VENDOR=onirent` 時不要把 `SOURCE=ws` 寫成唯一路徑；WS 不通時應用固定／隧道 ADB。

可選（證據能力，與裝置部署正交）：

```env
# 真 network_trace → har 投影；預設關
# ALIOTH_APP_NETWORK_PROXY_ENABLED=1
```

---

## 6. 部署步驟

### 6.1 選定拓撲並準備連線

**拓撲 A**

```powershell
adb connect <公網host>:<port>
adb devices   # 應見 device，而非 offline
```

**拓撲 B**

```powershell
# 負責人機
powershell -ExecutionPolicy Bypass -File F:\aliothapp\infra\scripts\start-linode-adb-tunnel.ps1
# 建議同時開守護，禁止公網雙掛
powershell -ExecutionPolicy Bypass -File F:\aliothapp\infra\scripts\guard-linode-adb-jump-only.ps1
```

Alioth 機驗證：

```powershell
adb connect 172.104.172.119:30615
adb -s 172.104.172.119:30615 shell echo ok
```

### 6.2 寫入環境並重建服務

1. 將對應 example 合併進實際使用的 `infra/env/.env.local`（或生產 `.env`）。  
2. 確認 compose 覆蓋項與 example 一致（MoreLogin auto 全 0）。  
3. 重建：

```powershell
# 以當前倉庫實際 compose 檔名為準；示意：
docker compose -f infra/docker-compose.local-dual-port.yml up -d --force-recreate alioth-api alioth-worker
```

4. 跑遷移（若尚未含設備租約相關 revision）：按單機部署指南執行 `alioth-migrate`。

### 6.3 設備庫存與殘留清理

1. 啟動後由池種子寫入／刷新 `qc_device_resource`（`device_ref=cloud_phone:qa-*`）。  
2. **關閉歷史 MoreLogin 資源**（避免誤選機）：

```sql
UPDATE qc_device_resource
SET enabled = false, health_state = 'disabled'
WHERE vendor = 'morelogin' OR capabilities->>'vendor' = 'morelogin';
-- 實際欄位以當前 schema 為準；原則：morelogin 行不得再參與 claim
```

3. 無有效租約卻長期 `claimed`／`recovering` 時，先查 `qc_device_lease`，再釋放或走工作台「解除隔離」；禁止只靠反覆 `docker restart`。

### 6.4 冒煙驗收（必須過才算部署完成）

| 步驟 | 預期 |
|------|------|
| `adb devices` 對池內每台 | `device` |
| 工作台裝置列表 | qa-* `health=available` |
| 上傳 APK → 指定／自動 claim 一台 | 任務 `running`，非 45s 內 `failed` |
| 截圖證據 | 非全白；`package_name` 有值 |
| 雙任務搶同池（可選） | 一台一租約；池滿有界等待或排隊 |
| 連續失敗 | 單線 quarantine 不拖死整池 |

輔助腳本（倉庫內）：

- `scripts/verify-cloud-phone-adb.ps1`  
- `scripts/smoke_cloud_phone_dual_line.py`  
- `scripts/run-cloud-phone-ai-smoke.py`（視覺 POC，非正式必跑）

---

## 7. 容量與調度建議

| 項 | 建議 |
|----|------|
| 槽位數 | 先 2（qa-a／qa-b）穩跑，再擴到 5 |
| 並行任務 | ≤ 槽位數；超槽只告警／等待，禁止盲目 powerOn |
| 單任務牆鐘 | 常態可 >55min；監控腳本 `POLL` 需大於任務預算 |
| 回收 | Worker 週期 `recover_expired`；終態必釋放；暫態 502 **release 不 quarantine** |
| 故障隔離 | 單機連續 shell／截圖失敗再 quarantine；先查 ADB／隧道，勿只重啟 Docker |

---

## 8. 運維紅線（高頻回歸）

1. **勿公網＋跳板雙 `adb connect` 同一台**；負責人機勿讓 ONIRENT GUI 長掛公網 ADB 搶 5037。  
2. **切 ONIRENT 後必須清 MoreLogin 開機／選機殘留**（compose＋DB）。  
3. 連續 ADB 502／offline：先查廠商／隧道／雙掛，**禁止**只重啟容器當根治。  
4. `qa-b` 等槽位無租約卻卡 `claimed`：DB 釋放＋解除隔離，避免 watchdog 死循環重啟。  
5. 異地共用只走跳板本機埠；勿把跳板當「登上去跑 Alioth」；Alioth **不部署在跳板上**。  
6. 安全：隧道埠已對公網監聽時應加防火牆／白名單；勿再散佈跳板 root 密碼。

---

## 9. 回滾

| 場景 | 動作 |
|------|------|
| 跳板不穩 | 臨時切拓撲 A（僅本機可達時）；或縮池到可連槽位 |
| 誤開 MoreLogin | 立刻把 auto 全關、清空 env_id、recreate、禁用 morelogin 庫存行 |
| 整池不可用 | `DEVICE_BACKEND` 勿隨便改回模擬器當「正式」；僅 sandbox 用 `redroid` 實驗 |
| 壞遷移／壞租約 | 停收新 App 任務 → 釋放 lease → 修種子 → 再放量 |

---

## 10. 交付檢查表（複製用）

- [ ] 拓撲已選定（A 或 B），無雙線搶連  
- [ ] env／compose：`VENDOR=onirent`，MoreLogin auto 全 0  
- [ ] `SLOTS` 與 `ONIRENT_ADB` 等長同序  
- [ ] api＋worker 已 force-recreate  
- [ ] 庫內 morelogin 資源已禁用  
- [ ] 池內每台 `adb shell echo ok`  
- [ ] 至少一筆真實 APK 任務跑到有截圖與包名  
- [ ] （異地）隧道腳本＋守護常駐；跳板防火牆策略已記錄  
- [ ] 費用與代理／時長責任人已確認  

---

## 11. 後續可選增強（不阻塞當前上線）

1. 跳板埠 IP 白名單／雲防火牆落地。  
2. 裝置健康大盤（offline／quarantine／租約年齡）。  
3. App 會話 HTTP 代理（`ALIOTH_APP_NETWORK_PROXY_*`）按需開啟，與 har 投影聯調。  
4. 播放器喚醒／剪貼板 URL 等能力與青雲對賬後再擴大規則覆蓋。
