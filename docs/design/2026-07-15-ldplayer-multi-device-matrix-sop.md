# Windows 雷電：同 APK 多機型／多解析度矩陣（SOP）

> 狀態：Demo 已落地（`mobile-demo-airtest-v40+`）  
> 主工作目錄：`F:\aliothG`  
> 關聯：`ALIOTH_LDPLAYER_PROFILES`、`mobile_demo` 矩陣 worker、`scripts/verify-ldplayer-profiles.ps1`

## 目標

上傳 **1 個 APK** → 自動對多個預建雷電實例（不同機型／解析度標籤）**依序各跑一輪** → 產出 `device_results` 與 `shots/{profile_id}/` 多組截圖。

## 預建實例（執行前）

1. 雷電多開：新建實例，分別設好解析度／DPI（例如 index0=1080×1920、index1=1280×720）。
2. 每台開啟 **ADB 本地連線**。
3. 用 `ldconsole list2` 確認 index 與名稱。
4. 填寫 `ALIOTH_LDPLAYER_PROFILES` 或 `ALIOTH_LDPLAYER_PROFILES_FILE`，與實際 index 對齊。  
   範例：`infra/env/ldplayer-profiles.example.json`；本機可用 `infra/env/ldplayer-profiles.local.json`。
5. 驗證：`powershell -File scripts/verify-ldplayer-profiles.ps1`

## 執行時行為

- **只切 `index`**，不呼叫 `ldconsole modify` 改解析度。
- 一輪矩陣內 **串行**（一台接一台）；單台失敗寫入該 `device_results`，繼續下一台。
- 頂層狀態：全成功 `succeeded`；任一失敗 `failed`，message 標明成功數／失敗數。
- Demo UI 依 `profile_id` 分欄顯示狀態與截圖。

## 配置片段

```json
[
  {"id": "phone_1080", "index": 0, "label": "手機 1080x1920", "width": 1080, "height": 1920},
  {"id": "tablet_1600", "index": 1, "label": "平板 1600x2560", "width": 1600, "height": 2560}
]
```

未配置時回退：單一 profile，`index=ALIOTH_LDPLAYER_INDEX`（預設 0）。

## 本階段不做

- 多矩陣彼此搶多 index 的並行策略擴展（矩陣會一次佔滿所需 index）
- 跑前動態 `modify` 解析度
- 正式 `qc_task`／Agent Tool 矩陣（僅 Demo 層）
