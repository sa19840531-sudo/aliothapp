# 2026-10-09：APP 创建任务包名哈希热修复

## 问题与修复范围

`POST /api/v2/tasks` 在构建 APP 探索目标快照时返回 500，异常为 `App exploration target hash 与内容不一致`。含大写字母的包名先参与哈希计算，随后快照校验器将 `allowed_packages` 转为小写，导致内容与哈希不一致；任务尚未进入提交与入队。

修复仅在 `build_app_exploration_target_snapshot` 计算哈希前按原校验规则规范化 `allowed_packages`。安装与运行使用的原始 `package_name` 保持原值，原有哈希完整性、异包名拒绝和历史读取协议保持。无数据库迁移，无前端菜单或路由同步。

## 验证

- 新增 8 个快照场景，覆盖混合大小写、小写、全部页面、显式首页、持久化往返和篡改拒绝；修改前 6 个大写场景失败，修改后全部通过。
- 两个现场包名分别经过完整质检与仅探索的真实 HTTP 入口，共 4 个回归；修改前全部返回 500，修改后全部返回 202。持久提交服务用记录替身，不创建业务任务。
- 相关快照、产品目标、读取、任务提交与入口测试共 146 条通过。另 1 条既有 Web 测试 `test_unknown_web_target_locks_dynamic_home_denominator` 的期望缺少 `collection_page`，已用 HEAD 原实现独立复验同样失败，本次未修改该范围。
- Ruff 代码检查与差异空白检查通过。目标源文件原有格式差异未扩大为本次无关修改。
- 线上补丁镜像先断网验证 8 个快照场景及 4 个真实应用入口，再在新 API 容器中重复校验。未创建线上巡检任务，因此不作为设备端巡检验收。

## 线上交付

2026-10-09 19:25（北京时间）仅替换 `192.46.230.40` 的 API，基于原线上镜像叠加一个源文件，未升级本地 HEAD 的其他改动。

| 项目 | 内容 |
| --- | --- |
| 线上基础提交 | `23ad12742c5ebbb186f4c0cc2d9c942ebeb72420` |
| 基础镜像 | `alioth-local/api:23ad12742c5ebbb186f4c0cc2d9c942ebeb72420` |
| 基础镜像 ID | `sha256:a83da2ff8c547ec773d89b2f47e08a119c94b5df0e2a95642927be2f18be0bd3` |
| 补丁镜像 | `alioth-local/api:package-hash-20261009-185fd1968799` |
| 补丁镜像 ID | `sha256:5cb1f100cc5ed91af9c2ff96292425f651f0abf110a73984f1f8b5c9d09aed6b` |
| 源文件 SHA256 | `185fd1968799184f94d62d0c0ddfef702f7254c8f351ef0d45902806a6c0e37b` |
| 现场交付记录 | `/opt/alioth/releases/hotfix-20261009-package-hash-185fd1968799/` |

交付持有 `/tmp/alioth-production-deploy.lock`，保留既有 Compose 参数与数据卷，只在 `/opt/alioth/config/docker-compose.app-qc.yml` 的 `alioth-api` 下加入补丁 `image`，原文件备份为交付目录中的 `compose-original.yml`；文件归属与权限保持 `root:alioth 0640`，全局 `compose.env` 哈希不变。

API 健康后通过 `nginx -t` 并平滑重载网关，刷新静态上游地址。内外网 `/api/v1/readyz` 均返回 `ready`，PG、Redis、证据存储与 Worker 检查正常。Worker、前端和网关容器身份、启动时间、重启次数在交付前后相同；Worker 仍为原镜像，未重启。交付日志同步写入 `/opt/alioth/releases/deployment-history.log`。

## 后续整版发布与回滚

**正式整版发布前须移除 `docker-compose.app-qc.yml` 中这次新增的 API `image` 覆盖及其说明注释，保留原有 `command` 和 `volumes`。** 否则该覆盖会优先于正式 Compose 的新镜像标签。整版发布仍走既有发布与验收流程。

需要立即回滚时，在同一发布锁内恢复 `compose-original.yml`，使用原来的两个 Compose 文件与环境文件，仅执行 `up -d --no-deps --force-recreate --wait --wait-timeout 240 alioth-api`，API 健康后再次检查并平滑重载网关、验证 readiness。原镜像已保留本机。若现场配置此后已有其他修改，不得直接用旧备份覆盖，应仅撤销本补丁的镜像字段。
