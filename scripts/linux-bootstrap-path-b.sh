#!/usr/bin/env bash
# Path B：在 VMware Ubuntu 訪客內準備 Docker + binderfs + 啟動主站與 ReDroid
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== preflight ==="
uname -a
test -e /dev/kvm || { echo "ERROR: /dev/kvm missing — enable VMware nested VT-x/EPT (vhv.enable=TRUE) and reboot guest"; exit 1; }
egrep -c '(vmx|svm)' /proc/cpuinfo | awk '{print "cpu_virt_flags="$1}'

# Docker Engine（若尚未安裝）
if ! command -v docker >/dev/null 2>&1; then
  echo "installing Docker Engine CE..."
  sudo apt-get update -y
  sudo apt-get install -y ca-certificates curl
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo usermod -aG docker "$USER" || true
fi

# 無 systemd 時 docker 可能需手動起；有 systemd 則 enable
if command -v systemctl >/dev/null 2>&1; then
  sudo systemctl enable --now docker || sudo service docker start || true
fi

# 當前 shell 可能尚未套用 docker 群組
if docker info >/dev/null 2>&1; then
  export DOCKER_BIN=docker
else
  export DOCKER_BIN="sudo docker"
fi
$DOCKER_BIN info >/dev/null

# binderfs + kvm 權限
bash "$ROOT/scripts/linux-prepare-redroid-host.sh"
sudo chmod 666 /dev/kvm 2>/dev/null || true
# 確保 binder 開機可掛（best-effort）
if ! mountpoint -q /dev/binderfs 2>/dev/null; then
  sudo mkdir -p /dev/binderfs
  sudo mount -t binder binder /dev/binderfs || true
fi
mountpoint -q /dev/binderfs && echo BINDERFS_OK || echo BINDERFS_WARN
ls /dev/binderfs 2>/dev/null | head -5 || true

# 主站 env：同機 Docker，不需 remote sync
ENV_LOCAL="$ROOT/infra/env/.env.local"
mkdir -p "$ROOT/infra/env"
if [[ ! -f "$ENV_LOCAL" ]]; then
  if [[ -f "$ROOT/infra/env/.env.example" ]]; then
    cp "$ROOT/infra/env/.env.example" "$ENV_LOCAL"
  elif [[ -f "$ROOT/infra/env/.env.emulator.example" ]]; then
    cp "$ROOT/infra/env/.env.emulator.example" "$ENV_LOCAL"
  else
    touch "$ENV_LOCAL"
  fi
fi

python3 - <<'PY'
from pathlib import Path
p = Path("infra/env/.env.local")
text = p.read_text(encoding="utf-8") if p.exists() else ""
kv = {
    "ALIOTH_DEVICE_BACKEND": "redroid",
    "ALIOTH_MOBILE_DEMO_HOST_EXECUTOR": "http://alioth-mobile-host:8765",
    "ALIOTH_MOBILE_DEMO_REMOTE_SYNC": "0",
    "ALIOTH_REDROID_ADB": "alioth-redroid:5555",
    "ALIOTH_WORKSPACE_HOST": "/workspace",
}
lines = text.splitlines()
keys = set(kv)
out = []
for line in lines:
    if not line or line.lstrip().startswith("#") or "=" not in line:
        out.append(line)
        continue
    k = line.split("=", 1)[0].strip()
    if k in keys:
        out.append(f"{k}={kv[k]}")
        keys.discard(k)
    else:
        out.append(line)
for k in kv:
    if k in keys:
        out.append(f"{k}={kv[k]}")
p.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
print("updated", p)
PY

echo "=== start docker emulator stack ==="
bash "$ROOT/scripts/start-docker-emulator-stack.sh"

echo "=== local health ==="
curl -sf http://127.0.0.1:8765/health || true
echo
curl -sf "http://127.0.0.1:8000/api/v1/mobile-demo/version?format=json" || true
echo
echo "PATH_B_GUEST_OK"
