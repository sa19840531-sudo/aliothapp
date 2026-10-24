# Linode 跳板 SSH 密钥（ADB 反向隧道）

| 文件 | 是否入库 | 说明 |
|------|----------|------|
| `alioth-linode-tunnel.pub` | 是 | 公钥；需出现在跳板 `~/.ssh/authorized_keys` |
| `alioth-linode-tunnel` | **否**（gitignore） | 私钥；仅本机持有，勿提交、勿贴群 |

## 常量（与代码一致）

- 跳板：`172.104.172.119`
- 用户：`root`（建议后续改为个人账号）
- 指纹：`SHA256:r95llCcyqRxYWJ9ApiBvaUTnarE+rHnsllsO3Du/WVI`
- 契约模块：`services/api/app/infrastructure/device/onirent_adb_tunnel.py`

## 本机安装私钥

```powershell
powershell -ExecutionPolicy Bypass -File infra/scripts/install-linode-ssh-key.ps1
```

会把仓库内私钥同步到 `%USERPROFILE%\.ssh\id_ed25519_alioth_linode`（若仓库已有私钥），
或反过来把 `~/.ssh` 私钥拷回 `infra/ssh/`（便于本机脚本默认找到）。

## 登录与开隧道

```bash
ssh -i infra/ssh/alioth-linode-tunnel root@172.104.172.119
```

```powershell
powershell -ExecutionPolicy Bypass -File infra/scripts/start-linode-adb-tunnel.ps1
```

登录跳板后：`adb connect 127.0.0.1:30615`（及其他 `3xxxx`）。详见 `docs/異地ADB跳板-接入說明.md`。
