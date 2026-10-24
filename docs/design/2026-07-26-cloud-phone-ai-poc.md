# 雲手機 + AI 視覺 POC（MoreLogin）

> 日期：2026-07-26（費用說明 2026-07-27 修訂）  
> 目標：用雲手機遠端 ADB 跑 Flutter App（避免**模擬器白屏**），並用 LLM 看圖點一步。

## 0. 為什麼不用模擬器、改用雲手機

| 點 | 說明 |
|----|------|
| 模擬器問題 | Docker／x86 模擬器上 Flutter 常出現 Skia／OpenGL 表面建立失敗 → **截圖全白**，質檢無法做 |
| 虛擬化衝突 | 本機模擬器全速與 Docker／WSL 搶 Hypervisor，難以同時穩定 |
| 雲手機解法 | ARM 真機級渲染 + 遠端 ADB，協定可複用；裝置不佔本機虛擬化 |

費用見下節；完整論證見 `2026-07-27-cloud-phone-technical-solution.md` §1.2～§1.3。

## 0. 費用先講清楚

| 項目 | 說明 |
|------|------|
| 雲手機免費額度 | **僅約 100 分鐘**（新用戶試用） |
| 超過 100 分鐘 | **開始產生雲手機相關費用**（按廠商計費） |
| SOCKS5 代理 | 開機**強制**；**需另行購買代理**，不含在 100 分鐘免費裡 |
| LLM | AI 路徑另計 OpenAI／相容閘道費用 |

完整方案見：`2026-07-27-cloud-phone-technical-solution.md` §2.3。

## 1. 架構

```text
Windows Host Runner / 本機腳本
  ├─ adb connect <CLOUD_PHONE_ADB>
  ├─ install / am start / screencap
  └─ AI smoke：截圖 → ALIOTH_LLM → tap/swipe
```

`ALIOTH_DEVICE_BACKEND=cloud_phone` 與 `redroid` 共用遠端 ADB 裝包／啟動邏輯。

## 2. MoreLogin 雲手機（試用 100 分鐘）

1. 註冊 [MoreLogin Remote Phone](https://www.morelogin.com/remote-phone)（新用戶約 **100 分鐘免費**；用完即計費）。
2. **購買並綁定 SOCKS5 代理**（否則無法開機）。
3. 建立／啟動一台雲手機，開啟 **ADB**，複製 `IP:埠`（與連線碼）。
4. 本機驗證：
   ```powershell
   adb connect <ip:port>
   adb devices
   ```

其他廠商只要最終能 `adb connect`，同一套腳本可用；費用與代理政策以該廠商為準。

## 3. 環境變數

複製片段進 `infra/env/.env.local`：

```env
ALIOTH_DEVICE_BACKEND=cloud_phone
ALIOTH_CLOUD_PHONE_ADB=x.x.x.x:5555
ALIOTH_MOBILE_DEMO_HOST_EXECUTOR=http://127.0.0.1:8765
ALIOTH_MOBILE_DEMO_REMOTE_SYNC=0

# AI smoke 需要
ALIOTH_LLM_PROVIDER=openai
ALIOTH_LLM_API_STYLE=chat_completion
ALIOTH_LLM_MODEL=gpt-4o-mini
ALIOTH_LLM_API_KEY=sk-...
# ALIOTH_LLM_BASE_URL=
```

視覺 POC 建議 `ALIOTH_LLM_API_STYLE=chat_completion`（圖文多模態較直覺）。

## 4. 驗收命令

```powershell
# ADB + 可選裝包白屏檢查
powershell -File scripts/verify-cloud-phone-adb.ps1

# AI 一步閉環（需 LLM Key 與額度）
python scripts/run-cloud-phone-ai-smoke.py
```

白屏門檻：截圖白像素比例 **低於 80%** 視為有畫面。

## 5. Demo 串接

```powershell
# 合併 env 後啟動本機 Host
powershell -File scripts/start-mobile-demo-cloud-phone.ps1
```

瀏覽本機 API Demo（依你實際 API 埠），上傳 APK；後端應顯示 `device_backend=cloud_phone`。

## 6. 已知限制

- **免費僅約 100 分鐘**；其後有雲手機費用，勿當無限免費裝置場。  
- **SOCKS5 代理需購買**；未綁定則無法開機。  
- 各廠 ADB 隧道可能需白名單。  
- AI 僅 POC 1～3 步，不取代整份 Airtest。
