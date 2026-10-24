# Alioth 雲手機 — 伺服器部署 Runbook

> 日期：2026-09-17  
> 目標主機：`192.46.230.40`（`/opt/alioth` 生產目錄）  
> 裝置面：ONIRENT 雲手機經 **Linode 跳板**（伺服器預設拓撲）  
> 總方案見：[`2026-09-17-cloud-phone-deployment-plan.md`](2026-09-17-cloud-phone-deployment-plan.md)  
> 單機 Compose 基線：[`2026-07-17-alioth-single-host-deployment-guide.md`](2026-07-17-alioth-single-host-deployment-guide.md)

---

## 0. 為什麼伺服器用跳板、不用本機 WS

| 選項 | 伺服器是否可行 | 說明 |
|------|----------------|------|
| CC 智控本機 WS `127.0.0.1:33332` | **否（正式）** | Linux 伺服器通常不跑 Windows ONIRENT 客戶端 |
| 公網直連 `*:2xxxx` | 可選 | 僅當伺服器防火牆／延遲穩定可達時；易與負責人雙掛衝突 |
| **跳板 `172.104.172.119:3xxxx`** | **推薦** | 負責人 Windows 掛 `-R`；Alioth 容器只連跳板埠 |

```text
負責人 Windows                     Linode 跳板                伺服器 192.46.230.40
 ONIRENT → 公網 ADB 2xxxx
        └─ ssh -R :3xxxx ────────► :3xxxx  ◄── alioth-worker 內 adb connect
```

Alioth **不部署在跳板上**；生產 Compose 只跑 api／worker／workstation／gateway。

---

## 1. 上線前檢查（負責人機 + 伺服器）

### 1.1 負責人 Windows（隧道源）

```powershell
powershell -ExecutionPolicy Bypass -File F:\aliothapp\infra\scripts\start-linode-adb-tunnel.ps1
powershell -ExecutionPolicy Bypass -File F:\aliothapp\infra\scripts\guard-linode-adb-jump-only.ps1
```

- 關閉 ONIRENT GUI 長掛公網 ADB（避免搶 5037）。  
- 跳板 `sshd`：`GatewayPorts clientspecified`；`30615` 等埠未被 DROP。

### 1.2 伺服器出網

從 `192.46.230.40` 必須能 TCP 連：

```text
172.104.172.119:30615
172.104.172.119:30660
172.104.172.119:30907
172.104.172.119:30033
172.104.172.119:30613
```

```bash
# 任選一槽探測（需 nc 或 timeout+bash）
timeout 3 bash -c 'cat < /dev/null > /dev/tcp/172.104.172.119/30660' && echo ok || echo fail
```

建議跳板防火牆只放行 **Alioth 伺服器公網 IP**。

### 1.3 鏡像含 adb

生產 API 鏡像（`infra/docker/api/Dockerfile.production`）已安裝 `platform-tools`；worker 與 api 同鏡像即可 `adb`。部署後驗證：

```bash
docker exec alioth-worker adb version
```

---

## 2. 寫入伺服器配置

路徑約定（與單機指南一致）：

| 檔案 | 用途 |
|------|------|
| `/opt/alioth/config/compose.env` | 鏡像 tag、網路名 |
| `/opt/alioth/config/alioth.env` | 業務密鑰 + 雲手機變數 |
| `/opt/alioth/config/docker-compose.app-qc.yml` | 可選 override（若存在會被 `deploy-production.sh` 自動掛上） |
| `/opt/alioth/current` | 當前釋出代碼／compose |

### 2.1 合併雲手機段落到 `alioth.env`

以 root 編輯（**禁止**把全文貼到聊天／工單）：

```bash
sudo -e /opt/alioth/config/alioth.env
```

追加／覆寫為下列契約（槽位可先縮成 qa-a／qa-b 兩台再擴五台）。完整模板已同步到倉庫：

`infra/env/.env.production.example`

```dotenv
ALIOTH_DEVICE_BACKEND=cloud_phone
ALIOTH_APP_QC_ENABLED=true
ALIOTH_APP_RUNTIME_PROVIDER=official

ALIOTH_APP_DEVICE_LEASE_BACKEND=postgres
ALIOTH_CLOUD_PHONE_VENDOR=onirent
ALIOTH_ONIRENT_ADB_SOURCE=tunnel
ALIOTH_ONIRENT_ADB_TUNNEL=1
ALIOTH_ONIRENT_ADB_TUNNEL_JUMP_HOST=172.104.172.119
ALIOTH_ONIRENT_ADB_TUNNEL_BIND=0.0.0.0
ALIOTH_ONIRENT_ADB_TUNNEL_MAP=51.89.164.208:20615=172.104.172.119:30615,51.195.137.27:20660=172.104.172.119:30660,198.244.190.180:20907=172.104.172.119:30907,51.195.137.27:20033=172.104.172.119:30033,51.89.164.208:20613=172.104.172.119:30613
ALIOTH_ONIRENT_WS_URL=
ALIOTH_DEVICE_POOL_SLOTS=cloud_phone:qa-a,cloud_phone:qa-b,cloud_phone:qa-c,cloud_phone:qa-d,cloud_phone:qa-e
ALIOTH_DEVICE_POOL_ONIRENT_ADB=172.104.172.119:30615,172.104.172.119:30660,172.104.172.119:30907,172.104.172.119:30033,172.104.172.119:30613
ALIOTH_CLOUD_PHONE_ADB=172.104.172.119:30615
ALIOTH_CLOUD_PHONE_ADB_AUTH=

ALIOTH_CLOUD_PHONE_MORELOGIN_API=adb
ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS=
ALIOTH_MORELOGIN_CLOUDPHONE_ID=
ALIOTH_MORELOGIN_LOCAL_API=
ALIOTH_MORELOGIN_OPEN_API=
ALIOTH_MORELOGIN_API_ID=
ALIOTH_MORELOGIN_API_KEY=
ALIOTH_MORELOGIN_ADB_AUTO_POWER_ON=0
ALIOTH_MORELOGIN_ADB_AUTO_POWER_OFF=0
ALIOTH_MORELOGIN_ADB_AUTO_REFRESH=0
ALIOTH_MORELOGIN_ADB_AUTO_ENABLE=0

ALIOTH_DEVICE_WAIT_MAX_SECONDS=600
ALIOTH_DEVICE_LEASE_RECOVERY_INTERVAL_SECONDS=5
ALIOTH_DEVICE_LEASE_RECOVERY_GRACE_SECONDS=60
ALIOTH_APP_LAB_MILESTONE=off
ALIOTH_APP_LAB_EXECUTES_PROBE=false
```

硬規則：

1. `SLOTS` 與 `ONIRENT_ADB` **等長同序**。  
2. 伺服器 **不要** 設 `SOURCE=ws` 當唯一選機源。  
3. 若曾開 MoreLogin：上述 auto 必須全 0，且庫存行禁用（見 §4）。  
4. Official／星語 Token 等既有變數保留；與雲手機 ADB 正交，缺一則 App 任務應 fail-closed。

權限：

```bash
sudo chown root:alioth /opt/alioth/config/alioth.env
sudo chmod 640 /opt/alioth/config/alioth.env
```

### 2.2（可選）Compose override 鎖定 MoreLogin 關閉

若歷史 override 曾強制 `MORELOGIN_*_AUTO_*=1`，在伺服器建立／修正：

`/opt/alioth/config/docker-compose.app-qc.yml`

```yaml
services:
  alioth-api:
    environment:
      ALIOTH_DEVICE_BACKEND: cloud_phone
      ALIOTH_CLOUD_PHONE_VENDOR: onirent
      ALIOTH_MORELOGIN_ADB_AUTO_POWER_ON: "0"
      ALIOTH_MORELOGIN_ADB_AUTO_POWER_OFF: "0"
      ALIOTH_MORELOGIN_ADB_AUTO_REFRESH: "0"
      ALIOTH_MORELOGIN_ADB_AUTO_ENABLE: "0"
      ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS: ""
  alioth-worker:
    environment:
      ALIOTH_DEVICE_BACKEND: cloud_phone
      ALIOTH_CLOUD_PHONE_VENDOR: onirent
      ALIOTH_MORELOGIN_ADB_AUTO_POWER_ON: "0"
      ALIOTH_MORELOGIN_ADB_AUTO_POWER_OFF: "0"
      ALIOTH_MORELOGIN_ADB_AUTO_REFRESH: "0"
      ALIOTH_MORELOGIN_ADB_AUTO_ENABLE: "0"
      ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS: ""
```

`deploy-production.sh` 若偵測到該檔會自動 `-f` 掛入。

---

## 3. 發布（拉碼／鏡像／遷移／重建）

在已具備 Registry 與備份腳本的前提下：

```bash
# 1) 更新 /opt/alioth/current 到含雲手機契約的提交（分支以實際為準，例 app-automation／dev_2.0）
sudo -u alioth -H bash -lc '
  cd /opt/alioth/current
  git fetch origin
  git checkout --detach <DEPLOY_COMMIT_SHA>
'

# 2) 標準發布（會備份 DB、migrate、initialize、up api/worker/web/gateway、verify）
cd /opt/alioth/current
sudo -u alioth -H bash infra/scripts/deploy-production.sh <DEPLOY_COMMIT_SHA>
```

僅改雲手機 env、鏡像未變時，至少 recreate worker／api 使環境生效：

```bash
COMPOSE='docker compose --env-file /opt/alioth/config/compose.env -f /opt/alioth/current/infra/docker-compose.production.yml'
# 若有 override：
# COMPOSE="$COMPOSE -f /opt/alioth/config/docker-compose.app-qc.yml"

$COMPOSE up -d --force-recreate alioth-api alioth-worker
```

---

## 4. 庫存與殘留清理（首次切 ONIRENT 必做）

進入 postgres（帳密以伺服器實際為準，勿回顯密碼）：

```bash
docker exec -it <postgres容器名> psql -U alioth -d alioth
```

```sql
-- 查看裝置庫存
SELECT device_ref, backend, health_state, enabled, capabilities
FROM qc_device_resource
ORDER BY device_ref;

-- 禁用歷史 MoreLogin 行（欄位名以實際 schema 為準；原則：不得再被 claim）
UPDATE qc_device_resource
SET enabled = false,
    health_state = 'disabled'
WHERE device_ref LIKE 'cloud_phone:%'
  AND (
    coalesce(capabilities->>'vendor','') = 'morelogin'
    OR coalesce(capabilities->>'morelogin_env_id','') <> ''
  );
```

無租約卻長期 `claimed`／`recovering`：

```sql
SELECT * FROM qc_device_lease WHERE released_at IS NULL;
-- 確認無活躍任務後再釋放／工作台「解除隔離」
```

禁止用 overnight watchdog 反覆 `docker restart` 當釋放手段。

---

## 5. 伺服器驗收（必須全部通過）

### 5.1 容器內 ADB

```bash
docker exec alioth-worker adb disconnect || true
docker exec alioth-worker adb connect 172.104.172.119:30615
docker exec alioth-worker adb connect 172.104.172.119:30660
docker exec alioth-worker adb devices -l
docker exec alioth-worker adb -s 172.104.172.119:30660 shell echo ok
```

預期：對應槽位為 `device`，`echo ok` 有輸出。

### 5.2 就緒與裝置池

```bash
curl -fsS http://127.0.0.1/api/v1/readyz
# 經 gateway；或容器內打 alioth-api:8000
```

工作台 → 裝置：qa-* 應為 `available`（或健康可 claim）。

### 5.3 真實 App 任務

1. 上傳測試 APK → 提交正式質檢（綁定／自動 claim 一台）。  
2. 任務進入 `running`，非數十秒內 `failed`。  
3. 截圖非全白；capture metadata 有 `package_name`。  
4. 終態後租約釋放，裝置回 `available`。

### 5.4 失敗時優先查

| 現象 | 先查 |
|------|------|
| `adb` offline／5037 | 負責人隧道是否斷；是否公網雙掛；跳板防火牆 |
| 任務立刻失敗「裝置／協定」 | `alioth.env` 是否生效；worker 是否 recreate；Official Token |
| 選到 MoreLogin／錯機 | DB morelogin 行是否仍 enabled；compose override |
| 池滿一直等 | `DEVICE_WAIT_MAX_SECONDS`；是否槽位數 < 並行任務 |
| 長卡 progress≈99 | 任務／finalization／閘門，不是「沒部署雲手機」 |

---

## 6. 日常運維

| 項 | 動作 |
|----|------|
| 隧道斷線 | 負責人重跑 `start-linode-adb-tunnel.ps1`＋守護；伺服器側 `adb devices` 復檢 |
| 擴容槽位 | 同步加長 `SLOTS`＋`ONIRENT_ADB`＋`TUNNEL_MAP` → recreate api／worker |
| 縮容 | 先停收任務 → 釋放租約 → 再改短列表 |
| 發布 | 繼續走 `deploy-production.sh`；雲手機變數留在 `alioth.env`，勿寫進 Git |
| 回滾裝置面 | 可臨時縮池到可連槽位；**不要**把生產 `DEVICE_BACKEND` 改回模擬器充當正式質檢 |

---

## 7. 交付檢查表（伺服器）

- [ ] 負責人隧道＋守護常駐；無公網雙掛  
- [ ] 伺服器 → 跳板五埠 TCP 通  
- [ ] `alioth.env` 已寫 ONIRENT tunnel 契約；MoreLogin 全關  
- [ ] （若有）`docker-compose.app-qc.yml` 不再強制 MoreLogin auto＝1  
- [ ] `deploy-production.sh` 或 force-recreate api／worker 已完成  
- [ ] `docker exec alioth-worker adb` 對池內機 `device`  
- [ ] DB morelogin 庫存已禁用  
- [ ] 至少一筆真實 APK 任務出圖且有包名  
- [ ] 跳板防火牆白名單已記錄責任人  

---

## 8. 與本機開發的差異（避免抄錯）

| 項 | 本機 Windows | 伺服器 Linux |
|----|--------------|--------------|
| 連線 | 可直連／可跳板 | **預設跳板** |
| WS | 可選 | 正式關閉依賴 |
| Compose | `docker-compose.local-dual-port.yml` 等 | `docker-compose.production.yml` |
| 配置 | `infra/env/.env.local` | `/opt/alioth/config/alioth.env` |
| 發布 | 本地 up | `infra/scripts/deploy-production.sh` |
