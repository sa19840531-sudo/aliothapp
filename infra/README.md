# infra/ 部署与运维配置

本目录存放 Alioth 平台的容器编排、生产镜像、环境变量样例与基础设施初始化脚本。

## 开发与生产边界

| 文件 | 用途 | 中间件策略 |
| --- | --- | --- |
| `docker-compose.yml` | 本地开发与 DevUI 联调 | 本地启动 PostgreSQL、Redis，挂载源码并运行开发服务 |
| `docker-compose.production.yml` | 测试/验收/生产发布 | 只部署应用容器，通过外部 Docker 网络复用已部署的 PostgreSQL、Redis、MinIO |

生产环境禁止使用 `docker-compose.yml`，禁止源码挂载、`uvicorn --reload`、Vite dev server 和容器启动时安装依赖。完整操作见[单机 Docker Compose 部署操作指南](../docs/deployment/2026-07-17-alioth-single-host-deployment-guide.md)。

## 第一阶段容器范围

第一阶段按照项目落地路线规划为 5 个常驻容器；另提供一个可选的本地开发调试容器 `alioth-devui`，通过 Compose profile 手动启用，不随默认启动进入业务链路：

| 容器 | 阶段对应 | 职责 |
| --- | --- | --- |
| `alioth-api` | M1/M3 | Python 3.12 + FastAPI 后端运行容器 |
| `alioth-postgres` | M2/M3 | PostgreSQL 主数据库，预留 pgvector 向量检索能力 |
| `alioth-redis` | M4 | 任务队列、缓存、分布式锁等异步执行基础设施 |
| `alioth-worker` | M4 | 独立 Worker 进程：消费 `alioth-redis` 中的 Agent Run 队列，驱动 Agent Loop 执行（详见 [services/api/app/worker.py](../services/api/app/worker.py)），与 `alioth-api` 共享同一份代码/镜像，仅入口命令不同 |
| `alioth-web` | M6 预留 | Vue3 / Vite 前端运行容器 |
| `alioth-devui` | 本地开发调试，可选 profile | Microsoft Agent Framework DevUI 调试入口：在 DevUI 对话中提交 URL，代理调用 Alioth 现有任务 API，由 `alioth-worker` 按正式链路执行 |

本地开发 Compose 暂不单独启动 MinIO 和 Nginx；生产 Compose 通过 `minio_net` 使用外部 MinIO，并使用 `alioth-gateway` 作为统一入口。Playwright 仍运行在 `alioth-worker` 内，暂不拆独立 `browser-worker`。

## Android 模拟器 Docker 整合（青云客方向）

在 **Linux + KVM + binderfs** 宿主上，用 overlay 把安卓模拟器纳入同一套 compose（对齐青云客「标准运行结构」）：

| 青云客示意 | Alioth 服务 | profile |
| --- | --- | --- |
| Android Emulator Container | `alioth-redroid`（可选 `alioth-redroid-2`） | `emulator` / `emulator-multi` |
| Appium Container | `alioth-appium`（预留） | `appium` |
| 自动化测试 Agent | `alioth-api` + `alioth-worker` + `alioth-mobile-host` | `emulator`（mobile-host） |
| PostgreSQL / Redis | `alioth-postgres` / `alioth-redis` | 默认 |

```bash
bash scripts/linux-prepare-redroid-host.sh
bash scripts/start-docker-emulator-stack.sh
bash scripts/verify-docker-emulator.sh
```

设计说明：[`docs/design/2026-07-20-docker-android-emulator-integration.md`](../docs/design/2026-07-20-docker-android-emulator-integration.md)  
编排：`docker-compose.emulator.yml`、样例 `env/.env.emulator.example`  
**Windows Docker Desktop 不建议跑 ReDroid**；本机仍用雷电 + Host Runner。

## 目录结构

```text
infra/
  docker-compose.yml
  docker-compose.production.yml
  env/
    .env.local.example
    .env.test.example
    .env.production.example
    compose.production.example
  docker/
    api/
      Dockerfile
      Dockerfile.production
    devui/
      Dockerfile
    web/
      Dockerfile
      spa.conf
    workstation/
      Dockerfile
    admin/
      Dockerfile
    gateway/
      Dockerfile
      nginx.conf
  scripts/
    deploy-production.sh
    redeploy-local-production.sh
    verify-production.sh
  postgres/
    init/
      01-enable-pgvector.sql
```

## Python 版本约定

Python 版本固定为 3.12+，定义在两处：

- 根目录 `.python-version`：供本地 Python 版本管理工具识别。
- 根目录 `pyproject.toml`：`requires-python = ">=3.12"`，并同步 Ruff 目标版本为 `py312`。
- `infra/docker/api/Dockerfile`、`infra/docker/api/Dockerfile.production` 与 `infra/docker/devui/Dockerfile`：开发后端、生产后端与 DevUI 容器基础镜像使用 `python:3.12-slim`。

## 环境变量样例

环境样例按运行环境和 Compose 参数拆分：

| 文件 | 用途 |
| --- | --- |
| `env/.env.local` | 本地真实配置，本地创建且不提交 |
| `env/.env.local.example` | 本地开发模板 |
| `env/.env.test.example` | 测试环境 |
| `env/.env.production.example` | API、Worker、Migration 和初始化任务的完整生产配置模板 |
| `env/compose.production.example` | Registry、镜像 Commit SHA、外部 Docker 网络和 Gateway 端口模板 |

真实密钥、密码、Token 不允许提交。落地部署时复制对应样例为真实 `.env` 文件，并由部署环境注入敏感值。

## Mobile Demo（原生 APP）与 Docker 的关系

Mobile Demo API 挂在现有 `alioth-api` 容器。**Airtest / 模拟器执行**有两种模式：

| 模式 | 适用 | 启动方式 |
| --- | --- | --- |
| **Windows 雷電（现网）** | 纯 ARM App 实测稳定 | `powershell -File scripts/start-mobile-demo-host.ps1` |
| **Docker ReDroid（Linux + KVM）** | 青云客方向：模拟器进 compose | `bash scripts/start-docker-emulator-stack.sh` |

Docker 模拟器 overlay：

```bash
# Linux 宿主（需 /dev/kvm）
bash scripts/linux-prepare-redroid-host.sh
bash scripts/start-docker-emulator-stack.sh
```

- 编排：`docker-compose.yml` + `docker-compose.emulator.yml`（profile `emulator`）
- 环境样例：`env/.env.emulator.example`
- 设计说明：[`docs/design/2026-07-20-docker-android-emulator-integration.md`](../docs/design/2026-07-20-docker-android-emulator-integration.md)

Windows Docker Desktop **无法**稳定跑 ReDroid（无 KVM）；Windows 开发请继续用雷電 + Host Runner。

- Host Runner接入与部署步骤：[APP自动化整体方案](../docs/design/2026-07-10-alioth-app-automation-overall-solution.md)
- 环境样例：`env/.env.local.example` 中 `ALIOTH_DEVICE_BACKEND`、`ALIOTH_MOBILE_DEMO_HOST_EXECUTOR` 等

## 启动方式

本地开发默认读取 `env/.env.local`。首次使用时先从模板复制一份真实本地配置，真实密钥只写入 `.env.local`：

```bash
copy infra\env\.env.local.example infra\env\.env.local

docker compose -f infra/docker-compose.yml up --build
```

生产部署不使用上述命令，必须使用生产 Compose、Commit SHA 镜像以及服务器独立环境文件。已启用 GitLab Registry 时使用 `deploy-production.sh`；现阶段由服务器本地构建镜像时，代码合并后使用：

```bash
sudo -u alioth -H \
  /opt/alioth/current/infra/scripts/redeploy-local-production.sh \
  --branch dev \
  --commit <FULL_COMMIT_SHA>
```

`dev` Push Pipeline 在测试通过后通过 SSH 自动调用上述命令。脚本会校验 Commit 属于远端 `dev`，并按“同步代码、构建四类镜像、备份、Migration、初始化、重建服务、基础验收、记录发布历史”的顺序执行。真实 Workstation 任务验收仍需单独执行，不能用基础健康检查代替。

首次手工部署的核心顺序为：

```bash
docker compose \
  --env-file /opt/alioth/config/compose.env \
  -f infra/docker-compose.production.yml \
  --profile ops pull

docker compose \
  --env-file /opt/alioth/config/compose.env \
  -f infra/docker-compose.production.yml \
  --profile ops run --rm alioth-migrate

docker compose \
  --env-file /opt/alioth/config/compose.env \
  -f infra/docker-compose.production.yml \
  --profile ops run --rm alioth-initialize

docker compose \
  --env-file /opt/alioth/config/compose.env \
  -f infra/docker-compose.production.yml \
  up -d alioth-api alioth-worker alioth-workstation alioth-gateway
```

如需使用测试环境样例：

```bash
docker compose --env-file infra/env/.env.test.example -f infra/docker-compose.yml up --build
```

## DevUI 开发调试入口

`alioth-devui` 是独立的开发期调试服务，只在启用 `devui` profile 时启动。它不会替代 `alioth-api`、`alioth-worker` 或正式任务接口；DevUI 对话里的 Debug Agent 只代理调用现有 API：

```text
DevUI 对话 -> POST /api/v1/tasks -> alioth-worker -> GET /api/v1/agents/runs/{run_id}
```

### 启动命令

先启动业务基础服务与 Worker：

```bash
docker compose -f infra/docker-compose.yml up -d alioth-postgres alioth-redis alioth-api alioth-worker
```

需要页面调试时，再单独启动 DevUI：

```bash
docker compose -f infra/docker-compose.yml --profile devui up alioth-devui
```

DevUI 不进入默认业务启动链路；生产环境不启用 `devui` profile 即可。DevUI 与业务容器一样默认读取 `infra/env/.env.local`，DeepSeek 等真实大模型配置应写在该文件中。不要在 `docker compose up` 命令末尾追加 `--tracing`，当前 DevUI 容器内部已经通过 `--instrumentation` 开启追踪能力。首次构建或 Dockerfile / 依赖变更时，可在 DevUI 命令中追加 `--build`；如只想后台运行 DevUI，可追加 `-d`。

### 访问地址

本地默认访问：

```text
http://localhost:8080
```

端口由 `ALIOTH_DEVUI_PORT` 控制，本地样例默认是 `8080`。DevUI 启用了 `--auth`，Bearer Token 来自 `DEVUI_AUTH_TOKEN`，本地样例默认值是：

```text
change-me-local-only
```

### 对话测试方式

打开 DevUI 后选择实体 `alioth_worker_debug`，输入类似：

```text
提交这个 URL 做质检：https://example.com，使用 alioth_first_ruleset@1。提交后告诉我 task_id 和 run_id，并查询 run 的执行状态。
```

预期流程：

1. DevUI Debug Agent 调用 `submit_url_task`，向 `alioth-api` 提交 `POST /api/v1/tasks`。
2. `alioth-worker` 从 Redis 队列消费该任务，并按正式 `run_main_agent(...)` 链路执行。
3. DevUI Debug Agent 调用 `get_run_status` 查询 `GET /api/v1/agents/runs/{run_id}`，返回 `status`、`termination_reason`、`step_count`、`error_message` 和 `action_trace`。
4. 如需查看业务结果，继续在 DevUI 对话中要求查询任务详情或规则执行结果，Debug Agent 会调用 `get_task_detail` 与 `get_rule_executions`。

也可以直接用 API 校验 Worker 写回结果：

```bash
curl http://localhost:8000/api/v1/agents/runs/<run_id>
```

注意：当前 `action_trace` 主要在 Worker Run 结束后写回查询结果；如果 Run 仍在执行中，DevUI 可能只能先看到 `running` 状态，实时逐步刷新需后续接入 Redis live trace 或 workflow 文件日志。

### 前置条件与边界

- DevUI 是开发期工具，不进入生产部署；生产环境不要启用 `devui` profile。
- DevUI Debug Agent 需要可用的 `ALIOTH_LLM_API_KEY` 等大模型配置，否则无法完成自然语言对话。
- URL 质检任务依赖数据库迁移与规则集种子数据；如果提交时报规则集不存在或数据表不存在，请先完成数据库初始化。
- DevUI 不直接访问数据库、Redis 或 Worker 内部函数，只通过 Alioth API 观察正式 Worker 执行结果。

## 验收口径

- `alioth-postgres` 启动后自动执行 `01-enable-pgvector.sql`，启用 `vector` 扩展。
- `alioth-redis` 可通过健康检查。
- `alioth-api` 可访问 `http://localhost:8000/api/v1/health`。
- `alioth-worker` 与 `alioth-api` 共享镜像，容器启动后日志应出现“Alioth Worker 已启动”字样，且能正常消费 `POST /api/v1/agents/runs` 或 `POST /api/v1/tasks` 提交的任务。
- `alioth-web` 当前保留 Vue3/Vite 运行入口；在 `app/workstation` 初始化真实 Vite 项目后会自动执行前端开发服务。
- `alioth-devui` 可通过 `--profile devui` 独立启动，访问 `http://localhost:8080` 后选择 `alioth_worker_debug`，提交 URL 后应能拿到 `task_id` / `run_id` 并查询 Worker 写回的 Run 状态。
