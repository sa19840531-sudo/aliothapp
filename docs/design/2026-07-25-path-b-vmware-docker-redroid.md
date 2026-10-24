# Path B：關 Hyper-V → VMware Ubuntu 承載 Docker Engine + ReDroid + 主站

> 日期：2026-07-25  
> 適用：本機 Windows 無法同時開 Hyper-V（Docker Desktop/WSL2）與全速 VMware/雷電時。

## 1. 為什麼要關

| Hypervisor ON | Hypervisor OFF |
|---------------|----------------|
| Docker Desktop / WSL2 可用 | **Docker Desktop / WSL2 掛掉** |
| VMware/雷電走 WHP，Android 慢 | VMware 拿原生 VT-x，可開巢狀 KVM |
| WSL 無 binderfs → ReDroid ADB 失敗 | Linux 訪客可掛 binderfs → **ReDroid ADB 可用** |

結論：主站與模擬器都搬進 **VMware Ubuntu**，Windows 只當瀏覽器客戶端。

## 2. 目標架構

```text
Windows（Hypervisor Off）
  └─ VMware Workstation
       └─ Ubuntu 64（vhv.enable=TRUE，建議 ≥6 vCPU / 16GB）
            └─ Docker Engine
                 ├─ alioth-postgres / redis / api / worker
                 ├─ alioth-redroid:5555   ← /dev/kvm + /dev/binderfs
                 └─ alioth-mobile-host:8765
瀏覽器 → http://<guest-ip>:8000/api/v1/mobile-demo/demo
```

同機 Docker 網路，**不需** `REMOTE_SYNC`。

## 3. 本機資產

| 項目 | 值 |
|------|-----|
| VMX | `F:\UB\Ubuntu 64 *.vmx` |
| 巢狀虛擬化 | `vhv.enable = TRUE`（已開） |
| 訪客 IP（慣例） | `192.168.33.128`（以實際 `ip a` 為準） |
| 部署腳本 | `scripts/deploy-alioth-to-vmware.py` |

現況 VM 僅 **2 vCPU / ~8GB**：一鍵腳本會在關機狀態把 vCPU 調到 **6**；建議你手動把記憶體調到 **16GB**。

## 4. 步驟

### 4.1 關 Hyper-V（管理員，需重開機）

```powershell
powershell -File F:\aliothG\scripts\disable-windows-hypervisor.ps1
shutdown /r /t 60 /c "Path B: disable Hypervisor for VMware ReDroid"
```

還原（之後要回 Docker Desktop）：

```powershell
powershell -File F:\aliothG\scripts\enable-windows-hypervisor.ps1
shutdown /r /t 60
```

### 4.2 重開機後一鍵啟動

```powershell
# 可選：ALIOTH_VM_HOST / ALIOTH_VM_USER / ALIOTH_VM_PASSWORD / ALIOTH_VMX_PATH
powershell -File F:\aliothG\scripts\start-path-b-vmware-docker.ps1
```

腳本會：檢查 Hypervisor 已關 → 啟動 VM → 等 SSH → 部署程式碼 → 訪客內 `linux-bootstrap-path-b.sh`（Docker / binderfs / `start-docker-emulator-stack.sh`）。

### 4.3 驗收

```text
http://192.168.33.128:8000/api/v1/mobile-demo/version?format=json
http://192.168.33.128:8765/health
adb connect 192.168.33.128:5555
```

訪客內亦可：`bash scripts/verify-docker-emulator.sh`

## 5. 風險與注意

1. **Windows Docker Desktop 會掛**：關 Hypervisor 後勿再依賴本機 `alioth-api` 容器。  
2. **WSL2 會掛**：先前 WSL Emulator 路徑暫廢。  
3. **巢狀 KVM 效能**：仍不如實體 Linux，但比 Hyper-V ON 時的 VMware 可用得多。  
4. **純 ARM 重遊戲包**：ReDroid 轉譯仍可能白屏；那種包繼續用雷電（Hypervisor Off 後雷電也會更順）。  
5. **密碼**：部署腳本預設讀環境變數；勿把真實密碼提交進 git。

## 6. 相關腳本

| 路徑 | 用途 |
|------|------|
| `scripts/disable-windows-hypervisor.ps1` | `hypervisorlaunchtype Off` |
| `scripts/enable-windows-hypervisor.ps1` | 還原 Auto |
| `scripts/start-path-b-vmware-docker.ps1` | 重開機後一鍵 |
| `scripts/linux-bootstrap-path-b.sh` | 訪客內 Docker+ReDroid+主站 |
| `scripts/start-docker-emulator-stack.sh` | compose 啟動 |
| `scripts/linux-prepare-redroid-host.sh` | binderfs / KVM |
| `scripts/deploy-alioth-to-vmware.py` | 同步程式碼到訪客 |
