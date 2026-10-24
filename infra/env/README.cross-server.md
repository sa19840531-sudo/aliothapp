# Alioth 跨服部署一覽（本環境實例）
#
# ## A) WSL2 沙盒（推薦，不依賴 VMware）
#
# | 角色   | 位置                         | 服務                                      |
# |--------|------------------------------|-------------------------------------------|
# | 主站   | Windows Docker               | API :8000, Postgres, Redis                |
# | 沙盒   | WSL2 Ubuntu-24.04            | Mobile Host :8765 → Android Emulator:5555 |
#
# 前置：VirtualMachinePlatform=Enabled（啟用後需重開機），WSL 內有 /dev/kvm 與 AVD。
#
# ```powershell
# powershell -File scripts/start-cross-server-wsl-sandbox.ps1
# powershell -File scripts/start-main-docker-cross-server-wsl.ps1
# powershell -File scripts/verify-cross-server-wsl.ps1
# ```
#
# 主站 env：infra/env/.env.cross-server.wsl → merge 進 .env.local
# 預期 version：remote_sync=true, host_executor=http://host.docker.internal:8765
#
# ## B) VMware UB 沙盒（備援）
#
# | 角色   | 機器              | IP              | 服務                          |
# |--------|-------------------|-----------------|-------------------------------|
# | 主站   | Windows (Docker)  | 192.168.33.1    | API :8000, Postgres, Redis    |
# | 沙盒   | UB VMware Ubuntu  | 192.168.33.128  | Mobile Host :8765, ReDroid :5555 |
#
# 主站 env：infra/env/.env.cross-server.main  → merge 進 .env.local
# 沙盒 env：infra/env/.env.cross-server.sandbox
#
# Demo：http://127.0.0.1:8000/api/v1/mobile-demo/demo
# 預期 version JSON：remote_sync=true, host_executor=http://192.168.33.128:8765
