"""在 F:\\UB Ubuntu（已有 KVM）安裝 Docker、同步模擬器檔、啟動 ReDroid stack。"""
from __future__ import annotations

import sys
import tarfile
import tempfile
from pathlib import Path

import paramiko

HOST = "192.168.33.128"
USER = "eevee"
PASSWORD = "1215627"
LOCAL_ROOT = Path(r"F:\aliothG")
REMOTE_DIR = "/home/eevee/alioth"


def ssh() -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        HOST,
        username=USER,
        password=PASSWORD,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    return c


def run(c: paramiko.SSHClient, cmd: str, timeout: int = 600) -> tuple[int, str]:
    print("====", cmd[:180], flush=True)
    _, so, se = c.exec_command(cmd, timeout=timeout)
    out = so.read().decode("utf-8", "replace") + se.read().decode("utf-8", "replace")
    code = so.channel.recv_exit_status()
    if len(out) > 5000:
        print(out[-5000:], flush=True)
    else:
        print(out, flush=True)
    print("exit", code, flush=True)
    return code, out


def sudo(c: paramiko.SSHClient, cmd: str, timeout: int = 900) -> tuple[int, str]:
    return run(c, f"echo {PASSWORD} | sudo -S bash -lc {repr(cmd)}", timeout=timeout)


def sync_files(c: paramiko.SSHClient) -> None:
    """同步 Docker 模擬器所需檔案（含 services 源碼供映像建置）。"""
    include_dirs = [
        "services",
        "infra/docker",
        "infra/env",
        "infra/postgres",
        "scripts",
        "docs/design",
    ]
    include_files = [
        "infra/docker-compose.yml",
        "infra/docker-compose.emulator.yml",
        "pyproject.toml",
        "README.md",
    ]
    exclude_dir_names = {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        ".venv",
        "runs",
    }
    exclude_suffixes = {".pyc", ".pyo", ".apk", ".png", ".docx"}

    with tempfile.TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / "patch.tgz"
        count = 0
        with tarfile.open(tar_path, "w:gz", format=tarfile.GNU_FORMAT) as tar:
            for rel in include_files:
                p = LOCAL_ROOT / rel
                if p.is_file():
                    tar.add(p, arcname=rel)
                    count += 1
            for d in include_dirs:
                root = LOCAL_ROOT / d
                if not root.exists():
                    print("skip missing dir", d, flush=True)
                    continue
                for p in root.rglob("*"):
                    if not p.is_file():
                        continue
                    rel_parts = set(p.relative_to(LOCAL_ROOT).parts)
                    if rel_parts & exclude_dir_names:
                        continue
                    if p.suffix in exclude_suffixes:
                        continue
                    if p.stat().st_size > 8_000_000:
                        continue
                    arc = p.relative_to(LOCAL_ROOT).as_posix()
                    tar.add(p, arcname=arc)
                    count += 1
        print(f"packed files={count} bytes={tar_path.stat().st_size}", flush=True)
        remote = f"/home/{USER}/patch.tgz"
        sftp = c.open_sftp()
        sftp.put(str(tar_path), remote)
        sftp.close()
        run(
            c,
            f"mkdir -p {REMOTE_DIR} && tar -xzf {remote} -C {REMOTE_DIR} && rm -f {remote} "
            f"&& chmod +x {REMOTE_DIR}/scripts/*.sh "
            f"&& test -f {REMOTE_DIR}/infra/docker-compose.emulator.yml && echo PATCH_OK",
        )


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    c = ssh()
    try:
        run(c, "ls -l /dev/kvm; docker --version 2>&1 || true; which docker || true")
        # 安裝 Docker（若無）
        code, _ = run(c, "command -v docker")
        if code != 0:
            print("Installing Docker...", flush=True)
            sudo(
                c,
                "export DEBIAN_FRONTEND=noninteractive; "
                "apt-get update -qq && "
                "apt-get install -y ca-certificates curl gnupg && "
                "install -m 0755 -d /etc/apt/keyrings && "
                "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && "
                "chmod a+r /etc/apt/keyrings/docker.gpg && "
                ". /etc/os-release && "
                "echo \"deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] "
                "https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable\" "
                "> /etc/apt/sources.list.d/docker.list && "
                "apt-get update -qq && "
                "apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin && "
                "usermod -aG docker eevee && "
                "systemctl enable --now docker && docker --version",
                timeout=1200,
            )

        sync_files(c)
        sudo(c, f"bash {REMOTE_DIR}/scripts/linux-prepare-redroid-host.sh")

        # 準備 .env.local
        run(
            c,
            f"mkdir -p {REMOTE_DIR}/infra/env; "
            f"cp -n {REMOTE_DIR}/infra/env/.env.local.example {REMOTE_DIR}/infra/env/.env.local 2>/dev/null || true; "
            f"cp {REMOTE_DIR}/infra/env/.env.emulator.example {REMOTE_DIR}/infra/env/.env.emulator.local; "
            f"grep -q '^ALIOTH_DEVICE_BACKEND=redroid' {REMOTE_DIR}/infra/env/.env.local || "
            f"printf '\\nALIOTH_DEVICE_BACKEND=redroid\\n"
            f"ALIOTH_MOBILE_DEMO_HOST_EXECUTOR=http://alioth-mobile-host:8765\\n"
            f"ALIOTH_REDROID_ADB=alioth-redroid:5555\\n' >> {REMOTE_DIR}/infra/env/.env.local; "
            f"echo ENV_OK",
        )

        # 用 sudo docker（避免尚未重新登入 docker group）
        sudo(
            c,
            f"cd {REMOTE_DIR}/infra && "
            f"docker compose --env-file env/.env.emulator.example "
            f"-f docker-compose.yml -f docker-compose.emulator.yml "
            f"--profile emulator up -d --build "
            f"alioth-postgres alioth-redis alioth-redroid alioth-mobile-host alioth-api",
            timeout=2400,
        )
        run(c, f"cd {REMOTE_DIR} && bash scripts/verify-docker-emulator.sh", timeout=300)
        sudo(c, "docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
