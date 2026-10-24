# Google Emulator 本機 POC 說明（Mobile Demo）

## 前置

1. 安裝 JDK 17+，以及 Android SDK Command-line Tools，設定 `ANDROID_HOME`
2. 安裝套件並建立 AVD：
   ```bash
   sdkmanager "platform-tools" "emulator" "platforms;android-30" "system-images;android-30;google_apis;x86_64"
   avdmanager create avd -n Alioth_API30 -k "system-images;android-30;google_apis;x86_64" -d pixel_4
   ```
3. Linux 建議開啟 KVM；無顯示器用無頭參數（程式預設已帶）
4. `pip install airtest`

### Windows 本機已裝路徑（開發機參考）

| 項目 | 路徑 |
|------|------|
| JDK | `C:\Program Files\Eclipse Adoptium\jdk-17.*` |
| SDK | `F:\Android\Sdk` |
| AVD | `Alioth_API30`（`%USERPROFILE%\.android\avd\`） |

使用者環境變數：`ANDROID_HOME` / `ANDROID_SDK_ROOT` = `F:\Android\Sdk`

## 啟用後端

### Windows + Docker API（推薦本機 POC）

1. `infra/env/.env.local`（或 compose 環境）設定：
   ```text
   ALIOTH_DEVICE_BACKEND=google_emulator
   ```
2. 重啟 `alioth-api` 容器
3. 另開終端啟動 Host（會帶 `ANDROID_HOME` 與自動起 AVD）：
   ```powershell
   powershell -File scripts/start-mobile-demo-google-emulator.ps1
   ```
4. 開啟 Demo：`http://127.0.0.1:8000/api/v1/mobile-demo/demo`

容器內無 Windows SDK；`google_emulator` 在 Docker 中會委派到 Host Runner，由本機跑 Emulator。

### Linux 本機直接跑 API

```bash
export ALIOTH_DEVICE_BACKEND=google_emulator
export ANDROID_HOME=/opt/android-sdk   # 依實際路徑
export ALIOTH_AVD_NAME=Alioth_API30
export ALIOTH_MOBILE_DEMO_FORCE_LOCAL=1
export ALIOTH_EMULATOR_AUTO_START=1
```

## 驗證

- `/api/v1/mobile-demo/version?format=json` → `device_backend: google_emulator`
- 執行日誌：`device backend=google_emulator`、`adb ready: emulator-xxxx`
- 切回雷電：`ALIOTH_DEVICE_BACKEND=ldplayer`，改跑 `scripts/start-mobile-demo-host.ps1`

## 注意

- 廣告腳本模板若依雷電解析度製作，Google Emulator 上可能需調整。
- Demo（v26+）在 `google_emulator` 後端會：**以 package 啟動 App**、跳過雷電桌面圖示 touch，並將 `wm size` 設為 `1080x1920` 以貼近廣告模板。
- 首次啟動 AVD 較慢（冷開機可達數分鐘）。
- Airtest 會綁定「已在跑的 adb.exe」；Host 會重啟 SDK adb server，避免誤用雷電 adb。
- **純 ARM APK**（僅 `arm64-v8a`）在 **x86_64** Emulator 上會經 `ndk_translation`，可能 SIGILL 閃退。
- **無 KVM / `-accel off`**：本機 Windows 實測進程會卡住（CPU 幾乎不漲、adb 長期 `offline`），無法完成 boot。Linux 雲主機若無 `/dev/kvm`，預期同樣不可用，不建議作為正式方案。
