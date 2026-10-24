# Docker Android 模擬器整合（青云客方向）

> 日期：2026-07-20（修訂）  
> 目的：採納青云客建議——**用 Docker 啟動安卓模擬器，統一納入 Alioth 專案環境**。  
> 關聯：`infra/docker-compose.emulator.yml`、`scripts/start-docker-emulator-stack.sh`

---

## 0. 與青云客「標準運行結構」對照

青云客示意（Linux 物理機 → Docker → 模擬器／自動化／資料面）：

```text
Linux 物理機
└─ Docker
   ├─ Android Emulator Container 1  ──► 本 repo：alioth-redroid
   ├─ Android Emulator Container 2  ──► 可選：alioth-redroid-2（profile emulator-multi）
   ├─ Appium Container              ──► 可選預留：alioth-appium（profile appium；Demo 現用 Airtest）
   ├─ 自動化測試 Agent 容器          ──► alioth-api + alioth-worker + alioth-mobile-host
   ├─ PostgreSQL / Redis            ──► alioth-postgres / alioth-redis
   └─ MinIO                         ──► 平台後續階段再加（見 infra/README；不阻塞本整合）
```

**對齊原則（與示意一致）：**

1. 模擬器是**獨立容器**，禁止塞進 `alioth-api` 鏡像。  
2. 自動化經 **ADB 網路連線**（`adb connect …:5555`）安裝／啟動 APK。  
3. 必須跑在 **Linux 宿主 + `/dev/kvm`**；**Windows + Docker Desktop 不建議**硬跑模擬器容器。  
4. Windows 開發機：雷電 + Host Runner；或連遠端 Linux 上的 Docker ReDroid。

---

## 1. 結論（給同事／青云客）

| 項目 | 是否可行 | 說明 |
|------|----------|------|
| Docker 起安卓模擬器 | **可行（Linux + KVM）** | **ReDroid 容器** + **Mobile Host 容器**（adb + Airtest） |
| 統一進 Alioth compose | **可行** | overlay：`docker-compose.yml` + `docker-compose.emulator.yml` |
| Windows Docker Desktop | **不建議** | 無穩定 `/dev/kvm`；Windows 用 **雷電 + Host Runner** |
| 多模擬器並行 | **可選** | `emulator-multi` 起第二台 `alioth-redroid-2`（第二階段矩陣） |
| Appium | **可選預留** | Demo 現用 Airtest；`profile appium` 可起容器共用同一 ADB |
| 純 ARM／重 GPU App | **有條件** | 可安裝；無實體 GPU 時可能白屏／Skia 失敗（見下文限制） |

Alioth 落地架構：

```text
┌─ Linux 宿主（KVM + binderfs）─────────────────────────────────┐
│  Docker network: alioth                                        │
│                                                                │
│  [資料面]  alioth-postgres · alioth-redis                      │
│  [控制面]  alioth-api · alioth-worker                          │
│  [裝置面]  alioth-mobile-host:8765  ──adb──► alioth-redroid:5555│
│            （可選 redroid-2 / appium）                         │
└────────────────────────────────────────────────────────────────┘
```

對應 ADB 驗收指令（與青云客示意相同語意）：

```bash
adb connect alioth-redroid:5555   # 容器網內；宿主則 127.0.0.1:5555
adb install -r app.apk
adb shell pm list packages
adb shell am start -n <package>/<activity>
```

---

## 2. 快速啟動（Linux）

```bash
# 1. 宿主準備（binder + KVM，需 sudo）
bash scripts/linux-prepare-redroid-host.sh

# 2. 一鍵起平台 + ReDroid + Mobile Host
bash scripts/start-docker-emulator-stack.sh

# 3. 驗證
bash scripts/verify-docker-emulator.sh
```

或手動：

```bash
cd infra
docker compose \
  --env-file env/.env.emulator.example \
  -f docker-compose.yml \
  -f docker-compose.emulator.yml \
  --profile emulator \
  up -d alioth-postgres alioth-redis alioth-redroid alioth-mobile-host alioth-api alioth-worker
```

Demo 頁：`http://<host>:8000/api/v1/mobile-demo/demo`  
預期 `device_backend=redroid`，日誌出現 `adb connect alioth-redroid:5555`。

### 可選：第二台模擬器

```bash
docker compose --env-file env/.env.emulator.example \
  -f docker-compose.yml -f docker-compose.emulator.yml \
  --profile emulator --profile emulator-multi \
  up -d alioth-redroid-2
# 宿主埠預設 5556 → 容器 5555
```

### 可選：Appium（預留，不影響現有 Airtest Demo）

```bash
docker compose --env-file env/.env.emulator.example \
  -f docker-compose.yml -f docker-compose.emulator.yml \
  --profile emulator --profile appium \
  up -d alioth-appium
# Appium: http://127.0.0.1:4723  capabilities 指向 alioth-redroid:5555
```

---

## 3. Windows 開發機

ReDroid **不要**在 Docker Desktop 硬跑。兩種方式：

**A. 維持現狀（推薦本機調腳本）**  
`ALIOTH_DEVICE_BACKEND=ldplayer` + `scripts/start-mobile-demo-host.ps1`

**B. 連 Linux 上 Docker ReDroid（統一集成驗收）**  
1. Linux 跑 `start-docker-emulator-stack.sh`，映射 `5555`  
2. Windows：`powershell -File scripts/start-mobile-demo-docker-redroid.ps1`  
3. `.env.local`：`ALIOTH_DEVICE_BACKEND=redroid`，`ALIOTH_REDROID_ADB=<linux-ip>:5555`

---

## 4. 環境變數

| 變數 | 預設 | 說明 |
|------|------|------|
| `ALIOTH_DEVICE_BACKEND` | `redroid`（emulator 模式） | API 走 ReDroid 後端 |
| `ALIOTH_MOBILE_DEMO_HOST_EXECUTOR` | `http://alioth-mobile-host:8765` | API 委派 Mobile Host |
| `ALIOTH_REDROID_ADB` | 容器內 `alioth-redroid:5555` | Host 側 adb 端點 |
| `ALIOTH_REDROID_PORT` | `5555` | 第一台映射到宿主 |
| `ALIOTH_REDROID_2_PORT` | `5556` | 第二台（emulator-multi） |
| `ALIOTH_WORKSPACE_HOST` | `/workspace` | Linux Docker 內勿填 `F:/...` |

樣例：`infra/env/.env.emulator.example`

---

## 5. 與 Appium／Airtest

| 層級 | 現狀 | 說明 |
|------|------|------|
| Demo 執行 | **Airtest**（`untitled.air`） | `alioth-mobile-host` 內執行 |
| 青云客示意 | Appium 容器 | 以 `profile appium` **預留**；與 Mobile Host **共用** `alioth-redroid:5555` |
| 正式 Tool | 適配器模式 | 對上 Agent 只暴露裝置 Tool；底層可換 Airtest／Appium |

---

## 6. 跨伺服器（沙盒 ↔ 主站）

模擬器沙盒與主站**分機部署**時，主站 API 不直接 adb，而是：

```text
主站 alioth-api
  │ ① POST /sync/{run_id}  推送 APK + run.json + log
  │ ② POST /run/{run_id}   委派執行
  │ ③ GET  /runs/{run_id}/snapshot + /screens/*  輪詢狀態與截圖
  ▼
沙盒 alioth-mobile-host:8765 → adb → alioth-redroid:5555
```

### 6.1 沙盒機（僅模擬器）

```bash
bash scripts/start-emulator-sandbox.sh
```

對外開放：`8765`（必須）、`5555`（可選）。

### 6.2 主站機（API + DB）

參考 `infra/env/.env.main-remote-emulator.example`：

```bash
ALIOTH_MOBILE_DEMO_HOST_EXECUTOR=http://<sandbox-ip>:8765
ALIOTH_MOBILE_DEMO_REMOTE_SYNC=1
```

### 6.3 驗證

```bash
SANDBOX_HOST=<sandbox-ip> MAIN_API_URL=http://<main-ip>:8000 \
  bash scripts/verify-cross-server-emulator.sh
```

### 6.4 同機 Docker（非跨服）

共用 `/workspace` 時設 `http://alioth-mobile-host:8765`，**不需** remote sync。

### 6.5 Windows + WSL 一體編排（Docker 沙盒 + 跨服，本機可測）

WSL 核心通常**無 binderfs**，ReDroid ADB 不可用；本機可測路徑改為：

```text
主站：Windows Docker（alioth-api）REMOTE_SYNC=1
        → host.docker.internal:8765
沙盒：WSL Docker Engine
        ├─ alioth-mobile-host（--network host）
        └─ Google Emulator（WSL /dev/kvm；暫代 ReDroid 裝置面）
```

一鍵：

```powershell
powershell -File scripts/start-docker-cross-server-integrated.ps1
powershell -File scripts/verify-cross-server-wsl.ps1
```

| 組件 | 承載 | 說明 |
|------|------|------|
| Mobile Host | **Docker**（WSL Engine） | 統一沙盒代理；協定與 Linux ReDroid 相同 |
| 模擬器 | WSL KVM Emulator | 待 Linux+binderfs 後可換回 `alioth-redroid` |
| 主站 | Windows Docker | `infra/env/.env.cross-server.wsl` |

完整 ReDroid 容器仍須 **Linux 物理機／VM（KVM + binderfs）**，見 §2／§7。

**本機單機若要同時滿足「Docker 起模擬器」與「VMware 全速」**：關 Windows Hypervisor，改走 Path B（VMware Ubuntu 內 Docker Engine + ReDroid），見 `docs/design/2026-07-25-path-b-vmware-docker-redroid.md`。

---

## 7. 已知限制（驗收前必讀）

| 限制 | 影響 | 建議 |
|------|------|------|
| 需 Linux + KVM + binderfs | Windows Desktop／WSL 無法穩定跑 ReDroid | 開發用 WSL Emulator 或雷電；集成／CI 用 Linux |
| WSL 無 binderfs | Docker ReDroid `adb connect` 失敗 | §6.5：Docker Host + Google Emulator |
| 重 GPU／部分 ARM 遊戲包 | 可裝、進程可在，但可能白屏無法點廣告 | 雷電或 ARM 真機／雲真機 |
| MinIO 尚未進 compose | 截圖暫存本機／run 目錄 | 不阻塞 Demo；對象存儲後續階段 |

---

## 8. 相關檔案

| 路徑 | 用途 |
|------|------|
| `infra/docker-compose.emulator.yml` | ReDroid + Mobile Host（+ 可選 multi／Appium） |
| `infra/docker/mobile-host/Dockerfile` | adb + airtest Host Runner |
| `infra/env/.env.emulator.example` | emulator 模式環境樣例 |
| `scripts/linux-prepare-redroid-host.sh` | KVM／binderfs |
| `scripts/start-docker-emulator-stack.sh` | Linux 一鍵啟動 |
| `scripts/verify-docker-emulator.sh` | 鏈路驗證 |
| `scripts/verify-cross-server-emulator.sh` | 主站↔沙盒連通驗證 |
| `scripts/start-docker-cross-server-integrated.ps1` | Windows+WSL 一體：Docker Host + Emulator + 跨服 |
| `scripts/wsl-start-docker-sandbox-integrated.sh` | WSL 側 Docker Host + KVM Emulator |
| `scripts/verify-cross-server-wsl.ps1` | WSL 跨服煙測 |
| `scripts/start-emulator-sandbox.sh` | 僅啟動沙盒 stack |
| `infra/docker-compose.emulator-sandbox.yml` | 沙盒專用 compose |
| `infra/env/.env.emulator-sandbox.example` | 沙盒環境樣例 |
| `infra/env/.env.main-remote-emulator.example` | 主站連遠端沙盒樣例 |
| `services/api/app/api/v1/mobile_demo_remote.py` | 跨服 sync/pull 邏輯 |
| `docs/design/2026-07-13-mobile-demo-deps-docker-integration.md` | Demo 依賴與 Docker 關係（總覽） |
