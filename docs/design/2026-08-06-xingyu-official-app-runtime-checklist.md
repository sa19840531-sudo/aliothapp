# 星語 Official App Runtime 實作清單（aliothapp）

> 日期：2026-08-06  
> 角色：你＝星語（裝置／安裝／開啟／截圖／動作）；青雲＝Workflow／Explorer／Capture 事實鏈  
> 對接客戶端：`aliothdev1.5` → `HttpOfficialAppApiClient`  
> 實作倉庫建議：`F:\aliothapp`（現有雲手機／ADB 能力最完整）

## 0. 目標與非目標

### 目標

青雲只打 HTTP；星語在本機／內網提供 Official App API，底層複用：

- `CloudPhoneController`（MoreLogin 喚醒、ADB 刷新、穩健 install、wm keep）
- `AdbDeviceOps` / `AndroidAdbSurfaceDriver`（tap／swipe／back／screencap）

### 非目標（本清單不做）

- 不把 `mobile-demo` 掛進生產 Gateway
- 不實作青雲 Completion Gate／Inspection／Rule Judge
- 不把 Airtest `untitled*` 當正式 Explorer（僅保留為已知 APK 回歸工具）

---

## 1. 青雲會打的契約（必須對齊）

預設路徑（可用環境變數覆寫，見青雲 `WorkflowSettings`）：

| 方法 | Path | 星語內部應對 |
| --- | --- | --- |
| `start_session` | `POST /v1/sessions` | MoreLogin powerOn（opt-in）+ ADB refresh + `ensure_ready` + 建立 `session_handle` |
| `install` | `POST /v1/apps/install` | 依 `platform_build_ref`+`sha256` 取 APK → `CloudPhoneController.install_apk` |
| `open` | `POST /v1/apps/open` | `prepare_app_for_script`（wm reset/keep + am start + 前景確認） |
| `capture_png` | `POST /v1/apps/capture` | `AdbDeviceOps.screencap`；回應 **PNG 二進位流** + 標頭 |
| `execute` | `POST /v1/apps/execute` | 解析 `ActionIntent` → Driver tap/scroll/back（座標 `canonical_px`） |
| `close` | `POST /v1/sessions/close` | 釋放 session／可選 force-stop；清 handle |

通用要求：

- Header：`Authorization`（或青雲配置的 `official_app_auth_header_name`）+ `Idempotency-Key`
- JSON 成功體至少含：`request_id`、業務結果欄位；失敗含 `error_code`、`retryable`
- Capture 成功標頭：`content-type`、`x-alioth-width`、`x-alioth-height`、`x-alioth-orientation`；可選 `x-alioth-package-name`、`x-alioth-activity`、`x-alioth-sha256`、`x-request-id`

參考實作（只讀）：[`F:\aliothdev1.5\services\api\app\infrastructure\app_runtime\official_api.py`](F:\aliothdev1.5\services\api\app\infrastructure\app_runtime\official_api.py)

---

## 2. 模組落點（建議目錄）

在 `aliothapp` 新增獨立路由前綴，**不要**掛在匿名 `mobile-demo` 上：

```text
services/api/app/
  api/v1/xingyu_official/          # FastAPI 路由（需 Auth）
    router.py
    schemas.py
  application/xingyu_runtime/      # 會話與冪等
    session_store.py
    idempotency.py
    service.py                     # 編排 CloudPhone / AdbDeviceOps
  infrastructure/xingyu/           # 可選：APK 下載／校驗
    build_artifact_fetch.py
```

掛載條件建議：

- `ALIOTH_XINGYU_OFFICIAL_ENABLED=1`
- 強制 Token：`ALIOTH_XINGYU_OFFICIAL_TOKEN`
- 預設只綁 loopback／內網；生產經專用 upstream，不走公開 Demo 閘道

---

## 3. 分階段實作清單

### Phase A — 契約殼 + 會話（約 1～2 天）

- [x] A1. 新增 `xingyu_official` router：6 個 POST，對上青雲契約形狀
- [x] A2. `session_store`：`session_id → {handle, serial, backend, created_at, package}`
- [x] A3. Auth 中介：比對 Token；無 Token 一律 401
- [x] A4. `Idempotency-Key` 表（記憶體）：同 key 同 payload 回快取；同 key 異 payload → 409
- [x] A5. 單元測試：未授權、冪等衝突、未知 session（`test_xingyu_official.py`）

### Phase B — start_session / close（接雲手機）

- [x] B1. `LiveCloudPhoneDeviceBridge.start` → `ensure_ready` + `open_adb_device_ops`
- [x] B2. `options.backend` 支援 `cloud_phone`（預設）；暫不開矩陣
- [x] B3. `close`：清 store；可選 `am force-stop`
- [x] B4. **禁止**任意 online `host:port` 回退（沿用整改）
- [x] B5. 日誌：backend／serial_set／reason（不打連線碼全文）

### Phase C — install / open

- [x] C1. APK：絕對路徑或 `ALIOTH_XINGYU_BUILD_CACHE`
- [x] C2. 校驗 `sha256` → `INSTALL_CHECKSUM_MISMATCH`
- [x] C3. `CloudPhoneController.ensure_apk_installed`
- [x] C4. `open` → `prepare_app_for_script`
- [x] C5. 回傳 `foreground_verified`／`activity`

### Phase D — capture / execute

- [x] D1. `capture_png`：screencap + 必填標頭
- [x] D2. `execute`：`tap`／`scroll`／`back`／`observe`；`input` → `ACTION_UNSUPPORTED`
- [x] D3. 座標：`CanonicalPointTargetResolver`／`map_point`
- [ ] D4. 硬禁區（可選 v1.1）
- [x] D5. `action_trace` 結構化

### Phase E — 與青雲聯調閘門

- [ ] E1. 青雲設 `ALIOTH_APP_RUNTIME_PROVIDER=official` + base URL
- [ ] E2. 跑青雲契約／聯調
- [ ] E3. 單任務煙測 Bootstrap → Capture
- [x] E4. 清單與回歸規則已更新

### 環境變數（星語側）

| 變數 | 說明 |
| --- | --- |
| `ALIOTH_XINGYU_OFFICIAL_ENABLED` | `1` 才掛載 `/v1/*` |
| `ALIOTH_XINGYU_OFFICIAL_TOKEN` | 必填；對齊青雲 `ALIOTH_OFFICIAL_APP_AUTH_TOKEN` |
| `ALIOTH_XINGYU_OFFICIAL_AUTH_HEADER` | 預設 `Authorization` |
| `ALIOTH_XINGYU_OFFICIAL_ALLOW_NON_LOOPBACK` | 隔離網顯式放行 |
| `ALIOTH_XINGYU_OFFICIAL_DRY_RUN` | `1` 不碰真機（測試／契約） |
| `ALIOTH_XINGYU_BUILD_CACHE` | APK staging 目錄 |

---

## 4. 現有能力 → Official 映射

| 已有模組 | Official 用途 |
| --- | --- |
| `morelogin_adb.refresh_cloud_phone_adb_from_morelogin` | `start_session` 前置 |
| `CloudPhoneController.ensure_ready` | 會話就緒 |
| `CloudPhoneController.install_apk` | `install` |
| `CloudPhoneController.prepare_app_for_script` | `open` |
| `AdbDeviceOps.screencap/tap/swipe/back` | `capture`／`execute` |
| `AndroidAdbSurfaceDriver` | in-process 路徑可直接持有；HTTP 層編排同一原語 |
| `mobile-demo` / `untitled*` | **僅**人工回歸；正式任務不呼叫 |

---

## 5. 建議實作順序（開工即做）

1. **Phase A** 契約殼（讓青雲能連上並過 Auth／形狀）  
2. **Phase B+C** 雲手機 start／install／open（你已有 80% 裝置邏輯）  
3. **Phase D** capture／execute（接 `AdbDeviceOps`）  
4. **Phase E** 與 `aliothdev1.5` official provider 聯調  

並行注意：

- Demo 繼續用 `ALIOTH_MOBILE_DEMO_ENABLED=1` 本機驗證裝置；**正式任務只打 `/v1/*` Official**  
- `AUTO_POWER_ON` 保持 opt-in；審計記錄誰觸發開機  

---

## 6. 驗收標準（星語側）

- 未帶 Token：全部 401  
- 同一 `Idempotency-Key` 重放：不重複 install／不重複開兩次計費開機  
- 真實雲手機：start → install → open → capture → tap → capture → close 全綠  
- 回應永不含本機 `storage_path`／ADB 連線碼全文  
- 青雲側可把 `ALIOTH_APP_RUNTIME_PROVIDER=official` 指到星語 base URL 跑通 Bootstrap（Completion Gate 仍屬青雲）

---

## 7. 與審查報告的關係

| 審查項 | 星語責任 |
| --- | --- |
| Blocker-2 Demo 匿名 | 已在 aliothapp 收口；Official **另開**受 Auth 路由 |
| P1-7 Demo Adapter 不足 | 用 Official + Session 串行／冪等補齊生產邊界 |
| 第六節 MoreLogin／install | 已修；收進 `start_session`／`install` |
| Blocker-1／P1-2 Gate | **青雲**；星語只保證 Capture／動作結果契約正確 |

下一步若要寫碼：從 **Phase A（router + Auth + session_store + 空殼 6 API）** 開始即可。

---

## 8. 與青雲聯調落地順序（2026-08-06）

1. 星語起 API：`ALIOTH_XINGYU_OFFICIAL_ENABLED=1` + Token；真機關 `DRY_RUN`。
2. APK 入庫：`scripts/stage-xingyu-apk.ps1 -ApkPath <apk> -BuildId <uuid>` → cache 內 `{uuid}.apk`。
3. 青雲環境：見 `docs/design/xingyu-qingyun-official-local.env.example`（base URL 指星語、Token 一致）。
4. 合同測試：青雲倉庫執行 `pytest ... -k star_live_contract`。
5. 已對齊缺口：`click`→`tap`；`resolved_target`→`point(device_px)`；`app-build:<uuid>` 快取別名。
6. 完整 App 任務仍需青雲 Screenshot Understanding Provider（非星語範圍）。
