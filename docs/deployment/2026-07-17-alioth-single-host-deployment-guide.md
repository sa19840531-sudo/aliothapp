# Alioth 单机 Docker Compose 部署操作指南

> 版本：v2.0
> 日期：2026-07-17
> 适用分支：`dev`
> 部署服务器：`192.46.230.40`
> 部署目标：PostgreSQL、Redis、MinIO 已在该服务器独立运行，使用 Docker Compose 部署 Alioth 应用和前端，并接入 GitLab CI/CD。

## 1. 部署结论

Alioth 应用采用生产 Docker Compose 部署，不再把 PostgreSQL、Redis、MinIO 重复编排进应用 Compose。

生产 Compose 只管理以下容器：

| 服务 | 职责 | 常驻 |
| --- | --- | --- |
| `alioth-api` | FastAPI HTTP 服务 | 是 |
| `alioth-worker` | Redis 队列消费、Agent Loop、Playwright 页面质检 | 是 |
| `alioth-workstation` | 质检工作台 + 管理后台（同一个 SPA，管理后台在 `/admin`）静态站点 | 是 |
| `alioth-gateway` | Nginx 统一入口和 API 反向代理 | 是 |
| `alioth-migrate` | Alembic 数据库迁移 | 否，一次性任务 |
| `alioth-initialize` | 规则、产品、Capability Registry 初始化 | 否，一次性任务 |

目标访问地址：

```text
质检工作台：http://192.46.230.40/
管理后台：http://192.46.230.40/admin/
API 内部地址：http://alioth-api:8000/
API 外部入口：http://192.46.230.40/api/v1/
Radar 域名入口：http://xnwork.xyz/（证书签发并重载 Gateway 后切换为 https://xnwork.xyz/）
```

当前登录 Token 尚未进行服务端签名，历史 Migration 还包含默认管理员密码。本版本只能部署到受控内网或测试/验收环境；完成鉴权加固前，不得直接向公网开放管理后台和业务 API。

## 2. 当前仓库状态

当前 [infra/docker-compose.yml](../../infra/docker-compose.yml) 是本地开发编排，不能直接作为生产部署文件，原因包括：

- 编排 PostgreSQL 和 Redis，会与服务器现有中间件重复。
- API 使用 `uvicorn --reload`。
- API、Worker 和前端挂载整个源代码目录。
- 前端使用 Vite dev server，并在启动时执行 `npm install`。
- 没有管理后台、生产 Nginx、Migration Job 和初始化 Job。
- 镜像没有稳定版本号和回滚口径。

本整改分支补齐以下生产部署文件，合并到部署基线后方可执行后续命令：

```text
.dockerignore
.gitlab-ci.yml
infra/
  docker-compose.production.yml
  env/
    .env.production.example
    compose.production.example
  docker/
    api/Dockerfile.production
    workstation/Dockerfile
    admin/Dockerfile
    web/spa.conf
    gateway/Dockerfile
    gateway/nginx.conf
  scripts/
    deploy-production.sh
    verify-production.sh
```

本文同时给出这些文件的职责、关键配置和部署命令。部署前仍需在 GitLab Runner 执行镜像构建和 Compose 校验，不能只依据文件存在判定部署已完成。

## 3. 部署架构

```text
浏览器
  |
  v
alioth-gateway :80
  |-- /api/* ---------> alioth-api:8000
  `-- / (工作台 + /admin 管理后台) --> alioth-workstation:80

alioth-api
  |-- data_services_net --> postgres:5432
  |-- data_services_net --> redis:6379
  `-- minio_net ---------> minio:9000

alioth-worker
  |-- data_services_net --> postgres:5432
  |-- data_services_net --> redis:6379
  |-- minio_net ---------> minio:9000
  `-- Internet ----------> LLM / 被测产品 URL
```

应用容器通过 Docker 外部网络访问同机中间件，不通过服务器公网 IP，也不需要公开 `5432`、`6379`、`9000`。

计划使用的网络：

| 网络 | 来源 | 成员 |
| --- | --- | --- |
| `alioth_app_net` | 应用 Compose 创建 | API、Worker、Workstation、Admin、Gateway |
| `data_services_net` | PostgreSQL/Redis Compose 已创建 | PostgreSQL、Redis、API、Worker、Migration、Initialize |
| `minio_net` | MinIO Compose 已创建 | MinIO、API、Worker |

## 4. 目录规划

```text
/opt/alioth/
  current/                 # Git 工作区，只存代码和部署声明
  config/
    compose.env            # Compose 镜像和非敏感部署参数
    alioth.env             # 应用真实环境变量和密钥
    acme-challenge/        # Let’s Encrypt HTTP-01 挑战目录
  releases/
    deployment-history.log # 发布记录
```

现有中间件目录继续保留：

```text
/opt/data-services/        # PostgreSQL、Redis、备份
/opt/minio/                # MinIO、mc.sh、Bucket 和策略
```

真实密钥只允许出现在 `/opt/alioth/config/alioth.env` 或 GitLab Protected Variables，不得进入 Git 仓库、Dockerfile、镜像 Layer 和普通部署日志。

TLS 证书由宿主机 `/etc/letsencrypt` 托管，Gateway 只读挂载，不进入仓库。

## 5. 部署前检查

### 5.1 服务器资源

首期最低建议为 4 核 CPU、8 GB 内存，推荐 8 核 CPU、16 GB 内存及以上。Worker 使用 Playwright Chromium，资源不足时页面采集和并发任务容易失败。

```bash
uname -a
cat /etc/os-release
nproc
free -h
df -h
```

### 5.2 Docker 环境

```bash
docker version
docker compose version
docker info
```

要求：

- Docker Engine 正常运行。
- 已安装 Compose V2，命令为 `docker compose`。
- 本地构建发布脚本需要宿主机 `python3`，使用标准库解析最终 Compose 配置并校验六个服务的镜像身份；不依赖宿主机 Python 第三方包。
- 部署用户可以执行 Docker 命令。
- Docker 数据目录磁盘空间充足。

确认 Gateway 计划使用的宿主机端口未被无关进程占用：

```bash
sudo ss -lntp | grep -E ':(80|443|8088)\b' || true
```

首次部署时，`80`、`443` 和 `8088` 必须空闲。升级已有 Alioth Compose 时允许由旧版
`alioth-gateway` 占用；若由宿主机 Nginx 或其他服务占用，应先停止无用服务，或在
`compose.env` 中修改 `ALIOTH_GATEWAY_HTTP_PORT`、`ALIOTH_GATEWAY_HTTPS_PORT`、`ALIOTH_GATEWAY_ADMIN_PORT` 并同步反向代理配置。

### 5.2.1 为 `xnwork.xyz` 准备 HTTPS

生产 Compose 默认约定：

- `443` 映射到 `alioth-gateway`
- 宿主机 `/etc/letsencrypt` 只读挂载到 Gateway
- 宿主机 `/opt/alioth/config/acme-challenge` 作为 HTTP-01 challenge webroot

首次签发证书命令：

```bash
sudo mkdir -p /opt/alioth/config/acme-challenge
sudo apt-get update
sudo apt-get install -y certbot

sudo certbot certonly \
  --webroot \
  -w /opt/alioth/config/acme-challenge \
  -d xnwork.xyz
```

证书签发后重建 Gateway：

```bash
cd /opt/alioth/current
docker compose \
  --env-file /opt/alioth/config/compose.env \
  -f infra/docker-compose.production.yml \
  up -d --force-recreate alioth-gateway
```

建议增加续期后的自动重载钩子：

```bash
sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-alioth-gateway.sh >/dev/null <<'SH'
#!/bin/sh
cd /opt/alioth/current
docker compose \
  --env-file /opt/alioth/config/compose.env \
  -f infra/docker-compose.production.yml \
  up -d --force-recreate alioth-gateway >/dev/null
SH
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-alioth-gateway.sh
```

### 5.3 中间件容器

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
docker network ls
```

确认外部网络存在：

```bash
docker network inspect data_services_net >/dev/null
docker network inspect minio_net >/dev/null
```

确认 PostgreSQL、Redis、MinIO 实际容器名和网络别名：

```bash
docker network inspect data_services_net
docker network inspect minio_net
```

本文默认使用以下 DNS 名称：

```text
postgres
redis
minio
```

如果实际容器名或网络别名不同，应调整 `/opt/alioth/config/alioth.env` 中的连接 URL，或在中间件 Compose 中增加稳定网络别名。不要依赖随机容器 IP。

### 5.4 从 Docker 网络验证中间件

PostgreSQL：

```bash
docker run --rm --network data_services_net \
  postgres:17-bookworm \
  pg_isready -h postgres -p 5432 -U alioth -d alioth
```

Redis：

```bash
docker run --rm -it --network data_services_net \
  redis:8-bookworm \
  redis-cli -h redis -p 6379 --askpass ping
```

预期输出：

```text
PONG
```

MinIO：

```bash
docker run --rm --network minio_net \
  curlimages/curl:8.10.1 \
  --fail --silent --show-error \
  http://minio:9000/minio/health/live
```

使用现有 MinIO Client 检查 Bucket：

```bash
cd /opt/minio
sudo ./mc.sh ls local/alioth
sudo ./mc.sh version info local/alioth
```

必须确认：

- `alioth` Bucket 已存在。
- 应用 Access Key 对该 Bucket 具备对象读、写、删除权限。
- MinIO API 使用 `9000`，不是管理控制台 `9001`。
- 长期环境建议启用 Bucket Versioning。

## 6. 生产镜像要求

### 6.1 `.dockerignore`

必须排除：

```dockerignore
.git
.gitignore
.env
.env.*
!.env.example
.venv
**/__pycache__
**/*.pyc
**/node_modules
**/dist
.pytest_cache
.ruff_cache
.alioth
docs/ui
```

目标是避免真实密钥、本地虚拟环境、Node 依赖和证据文件进入 Docker 构建上下文。

### 6.2 API 生产镜像

`infra/docker/api/Dockerfile.production` 至少需要：

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /workspace

COPY pyproject.toml README.md ./
COPY services ./services
COPY scripts ./scripts
COPY docs/data ./docs/data

RUN pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && useradd --system --create-home --uid 10001 alioth

USER 10001

EXPOSE 8000

CMD ["uvicorn", "services.api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

生产镜像必须包含：

- API 和 Worker 共用的 `services/`。
- Alembic Migration。
- `scripts/seed_first_ruleset.py`。
- `scripts/seed_products.py`。
- `scripts/sync_capability_registry.py`。
- `docs/data/` 下的规则集和产品种子数据。
- Playwright Chromium 和系统依赖。

禁止：

- 安装 `.[dev]`。
- 把 `.env` COPY 进镜像。
- 用 root 用户运行 API 和 Worker。
- 在容器启动时执行 `pip install`。

### 6.3 前端生产镜像

Workstation 和 Admin 都使用多阶段构建：

```dockerfile
FROM node:22-bookworm-slim AS build
WORKDIR /src

COPY app/workstation/package.json app/workstation/package-lock.json ./
RUN npm ci
COPY app/workstation/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY infra/docker/web/spa.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
```

管理后台已并入工作台同一个 SPA（路由前缀 `/admin`），不再单独构建镜像。

静态站点配置 `infra/docker/web/spa.conf`：

```nginx
server {
    listen 80;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

前端默认使用相对地址 `/api/v1`，由 Gateway 统一代理，不需要把服务器 IP 编译进前端镜像。

### 6.4 Gateway 镜像

`alioth-gateway` 负责两个入口和 API 代理：

```nginx
upstream alioth_api {
    server alioth-api:8000;
}

upstream alioth_workstation {
    server alioth-workstation:80;
}

server {
    listen 80;
    server_name _;

    location /api/ {
        proxy_pass http://alioth_api;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 10s;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        client_max_body_size 20m;
    }

    location / {
        proxy_pass http://alioth_workstation;
        proxy_set_header Host $host;
    }
}

```

## 7. 生产 Docker Compose 设计

`infra/docker-compose.production.yml` 使用 GitLab Registry 镜像，不挂载源代码：

```yaml
services:
  alioth-migrate:
    image: ${CI_REGISTRY_IMAGE}/api:${ALIOTH_IMAGE_TAG}
    container_name: alioth-migrate
    profiles: ["ops"]
    env_file:
      - /opt/alioth/config/alioth.env
    command:
      - python
      - -m
      - alembic
      - -c
      - services/api/migrations/alembic.ini
      - upgrade
      - head
    networks:
      - data_services_net

  alioth-initialize:
    image: ${CI_REGISTRY_IMAGE}/api:${ALIOTH_IMAGE_TAG}
    container_name: alioth-initialize
    profiles: ["ops"]
    env_file:
      - /opt/alioth/config/alioth.env
    command:
      - /bin/sh
      - -c
      - >-
        python scripts/seed_first_ruleset.py &&
        python scripts/seed_products.py &&
        python scripts/sync_capability_registry.py
    networks:
      - data_services_net

  alioth-api:
    image: ${CI_REGISTRY_IMAGE}/api:${ALIOTH_IMAGE_TAG}
    container_name: alioth-api
    restart: unless-stopped
    env_file:
      - /opt/alioth/config/alioth.env
    command:
      - uvicorn
      - services.api.app.main:app
      - --host
      - 0.0.0.0
      - --port
      - "8000"
      - --workers
      - "2"
    expose:
      - "8000"
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - >-
          import urllib.request;
          urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 20s
    networks:
      - alioth_app_net
      - data_services_net
      - minio_net

  alioth-worker:
    image: ${CI_REGISTRY_IMAGE}/api:${ALIOTH_IMAGE_TAG}
    container_name: alioth-worker
    restart: unless-stopped
    env_file:
      - /opt/alioth/config/alioth.env
    command:
      - python
      - -m
      - services.api.app.worker
    depends_on:
      alioth-api:
        condition: service_healthy
    stop_grace_period: 660s
    networks:
      - alioth_app_net
      - data_services_net
      - minio_net

  alioth-workstation:
    image: ${CI_REGISTRY_IMAGE}/workstation:${ALIOTH_IMAGE_TAG}
    container_name: alioth-workstation
    restart: unless-stopped
    expose:
      - "80"
    networks:
      - alioth_app_net

  alioth-gateway:
    image: ${CI_REGISTRY_IMAGE}/gateway:${ALIOTH_IMAGE_TAG}
    container_name: alioth-gateway
    restart: unless-stopped
    ports:
      - "80:80"
    depends_on:
      alioth-api:
        condition: service_healthy
      alioth-workstation:
        condition: service_started
    networks:
      - alioth_app_net

networks:
  alioth_app_net:
    name: alioth_app_net
  data_services_net:
    external: true
    name: ${DATA_SERVICES_NETWORK:-data_services_net}
  minio_net:
    external: true
    name: ${MINIO_NETWORK:-minio_net}
```

说明：

- PostgreSQL、Redis、MinIO 不在本文件中定义。
- `alioth-migrate` 和 `alioth-initialize` 使用 `ops` profile，不随常驻服务自动启动。
- Migration 和初始化由发布流程显式执行，失败时不得启动新版本。
- `alioth-api` 不映射宿主机端口，只允许 Gateway 和内部网络访问。
- `alioth-worker` 使用 660 秒优雅停止时间，覆盖当前 600 秒 Agent Run 上限。
- 生产环境禁止挂载仓库源代码。

## 8. 服务器部署配置

### 8.1 创建目录和部署用户

```bash
sudo useradd --system --create-home --shell /bin/bash alioth || true
sudo mkdir -p /opt/alioth/current /opt/alioth/config /opt/alioth/releases
sudo chown -R alioth:alioth /opt/alioth
sudo chmod 750 /opt/alioth/config
```

将部署用户加入 Docker 组：

```bash
sudo usermod -aG docker alioth
```

重新登录后验证：

```bash
sudo -u alioth -H docker ps
```

### 8.2 拉取部署声明

```bash
sudo -u alioth -H git clone \
  ssh://git@192.46.230.40:12424/tyzx/alioth.git \
  /opt/alioth/current
```

```bash
sudo -u alioth -H bash -lc '
  cd /opt/alioth/current
  git fetch origin +refs/heads/dev:refs/remotes/origin/dev
  git checkout --detach origin/dev
  git status --short --branch
'
```

### 8.3 Compose 参数

创建 `/opt/alioth/config/compose.env`：

```dotenv
CI_REGISTRY_IMAGE=alioth-local
ALIOTH_IMAGE_TAG=<DEPLOY_COMMIT_SHA>
DATA_SERVICES_NETWORK=data_services_net
MINIO_NETWORK=minio_net
```

`CI_REGISTRY_IMAGE` 必须以 GitLab 项目实际 Container Registry 地址为准。若 GitLab 尚未启用 Registry，先完成第 14 节的 Registry/Runner 准备，不能直接照抄示例端口。

```bash
sudo chown alioth:alioth /opt/alioth/config/compose.env
sudo chmod 640 /opt/alioth/config/compose.env
```

`compose.env` 不保存业务密钥，但发布脚本需要更新 `ALIOTH_IMAGE_TAG`，因此由部署用户持有；真正包含数据库、Redis、MinIO、LLM 密钥的 `alioth.env` 仍必须由 root 持有。

### 8.4 应用环境变量

PostgreSQL 和 Redis 密码包含特殊字符时，先进行 URL 编码：

```bash
python3 -c 'import getpass, urllib.parse; print(urllib.parse.quote(getpass.getpass("Password: "), safe=""))'
```

创建 `/opt/alioth/config/alioth.env`：

```dotenv
ALIOTH_APP_NAME=Alioth API
ALIOTH_ENVIRONMENT=production
ALIOTH_API_V1_PREFIX=/api/v1
ALIOTH_DEBUG=false
ALIOTH_CORS_ALLOWED_ORIGINS=http://192.46.230.40

ALIOTH_DATABASE_URL=postgresql://alioth:<URL_ENCODED_POSTGRES_PASSWORD>@postgres:5432/alioth

ALIOTH_REDIS_URL=redis://:<URL_ENCODED_REDIS_PASSWORD>@redis:6379/0
ALIOTH_QUEUE_RUN_TTL_SECONDS=86400
ALIOTH_AGENT_RUN_STATE_STORE_BACKEND=postgres

ALIOTH_WORKER_CONCURRENCY=4
ALIOTH_WORKER_POLL_TIMEOUT_SECONDS=5

ALIOTH_AGENT_MAX_STEPS=200
ALIOTH_AGENT_MAX_ITERATIONS=200
ALIOTH_AGENT_MAX_DURATION_SECONDS=3600
ALIOTH_AGENT_MAX_STEPS_HARD_CAP=600
ALIOTH_AGENT_STUCK_WINDOW=3
ALIOTH_AGENT_RETRY_MAX_ATTEMPTS=3
ALIOTH_AGENT_RETRY_BACKOFF_SECONDS=0.5

ALIOTH_LLM_PROVIDER=custom
ALIOTH_LLM_API_STYLE=chat_completion
ALIOTH_LLM_MODEL=grok-4.5
ALIOTH_LLM_API_KEY=<LLM_API_KEY>
ALIOTH_LLM_BASE_URL=https://api.routin.ai/v1
ALIOTH_LLM_TIMEOUT_SECONDS=180
ALIOTH_LLM_MAX_RETRIES=2
ALIOTH_LLM_USAGE_AUDIT_ENABLED=1
ALIOTH_LLM_COST_CURRENCY=CNY
ALIOTH_LLM_INPUT_PRICE_PER_MILLION_TOKENS=0.8
ALIOTH_LLM_OUTPUT_PRICE_PER_MILLION_TOKENS=2.4
ALIOTH_LLM_CACHE_HIT_PRICE_PER_MILLION_TOKENS=0.2
ALIOTH_LLM_LONG_CONTEXT_THRESHOLD_TOKENS=200000
ALIOTH_LLM_LONG_CONTEXT_INPUT_PRICE_PER_MILLION_TOKENS=1.6
ALIOTH_LLM_LONG_CONTEXT_OUTPUT_PRICE_PER_MILLION_TOKENS=4.8
ALIOTH_LLM_LONG_CONTEXT_CACHE_HIT_PRICE_PER_MILLION_TOKENS=0.4

ALIOTH_EVIDENCE_STORAGE_BACKEND=minio

# 证据清单读取 / 离场门禁 / 生命周期（生产默认 fail-closed）
ALIOTH_EVIDENCE_MANIFEST_READ_MODE=off
ALIOTH_WEB_EVIDENCE_EXIT_GATE_MODE=off
ALIOTH_EVIDENCE_RETENTION_DAYS=90
ALIOTH_EVIDENCE_GC_ENABLED=false
ALIOTH_MINIO_ENDPOINT=minio:9000
ALIOTH_MINIO_ACCESS_KEY=<MINIO_APP_ACCESS_KEY>
ALIOTH_MINIO_SECRET_KEY=<MINIO_APP_SECRET_KEY>
ALIOTH_MINIO_BUCKET=alioth
ALIOTH_MINIO_SECURE=false

PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
```

### 8.5 Official App Provider 与星语联调闸门

只有启用 App 质检并明确选择 Official Provider 时，才允许配置星语接口。未完整配置时，App
任务必须 fail-closed，不能降级到 H5 Main Agent 或 Fake Provider。以下变量只写入
`/opt/alioth/config/alioth.env` 或受保护的 CI 变量；认证 Token、APK URL、设备句柄和 PNG
二进制不得出现在 Git、日志、Redis 或 PostgreSQL。

```dotenv
ALIOTH_APP_QC_ENABLED=true
ALIOTH_APP_RUNTIME_PROVIDER=official
ALIOTH_APP_RUN_LEASE_SECONDS=180
ALIOTH_APP_RUN_RECOVERY_INTERVAL_SECONDS=5
ALIOTH_OFFICIAL_APP_API_BASE_URL=https://<STAR_PROVIDER_HOST>
ALIOTH_OFFICIAL_APP_AUTH_HEADER_NAME=Authorization
ALIOTH_OFFICIAL_APP_AUTH_TOKEN=<STAR_PROVIDER_TOKEN>
ALIOTH_OFFICIAL_APP_TIMEOUT_SECONDS=60
ALIOTH_OFFICIAL_APP_MAX_SCREENSHOT_BYTES=20971520
ALIOTH_OFFICIAL_APP_START_SESSION_PATH=/v1/sessions
ALIOTH_OFFICIAL_APP_INSTALL_PATH=/v1/apps/install
ALIOTH_OFFICIAL_APP_OPEN_PATH=/v1/apps/open
ALIOTH_OFFICIAL_APP_CAPTURE_PATH=/v1/apps/capture
ALIOTH_OFFICIAL_APP_EXECUTE_PATH=/v1/apps/execute
ALIOTH_OFFICIAL_APP_CLOSE_PATH=/v1/sessions/close
```

历史变量 `ALIOTH_OFFICIAL_APP_BASE_URL` 仍可兼容读取；两者同时存在时以
`ALIOTH_OFFICIAL_APP_API_BASE_URL` 为准。

Adapter 只接受 `image/png` 响应，并在读取过程中执行字节上限、PNG 文件头、尺寸和可选
SHA-256 校验。PNG 原文先进入对象存储；PostgreSQL 只保存 Capture、Artifact 与 App 元数据。
对象存储写入后数据库事务失败时，必须执行补偿删除；补偿失败记录
`app_capture_orphan_total`，由 sweeper 依据 Artifact 引用清理。

真实星语合同测试默认不执行。仅在隔离测试环境同时提供 Official 地址、认证变量并显式设置
`ALIOTH_OFFICIAL_APP_CONTRACT_TEST_ENABLED=true` 时才允许发起；缺任一变量必须安全跳过，
不得以 Fake Runtime 伪造联调成功。真实合同测试不连接 PostgreSQL；数据库验收另行使用与生产
库不同的 `ALIOTH_TEST_DATABASE_URL`，并在完成后清理测试对象存储前缀。

真实联调还需要由星语提供专用测试 APK 的三项非敏感标识，且不得使用生产 APK：

```dotenv
ALIOTH_OFFICIAL_APP_CONTRACT_PLATFORM_BUILD_REF=<TEST_BUILD_REF>
ALIOTH_OFFICIAL_APP_CONTRACT_PACKAGE_NAME=<TEST_PACKAGE_NAME>
ALIOTH_OFFICIAL_APP_CONTRACT_APK_SHA256=<TEST_APK_SHA256>
```

CI 使用受保护变量注入地址、Token 与上述测试标识后执行以下命令；命令只发起
`start-session → install → open → capture PNG → close-session`，不连接 PostgreSQL、不写对象存储，
也不打印响应体、PNG 或凭据：

```bash
ALIOTH_OFFICIAL_APP_CONTRACT_TEST_ENABLED=true \
python -m pytest services/api/tests/test_official_app_star_contract.py \
  -k star_live_contract -q
```

如果未向 CI/部署进程注入完整变量，上述测试显示 `SKIPPED` 是预期安全行为；不得改为 Mock 或
Fake Runtime 以消除 skip。

Adapter 日志只允许 `task_id`、`session_id`、`request_id`、`capture_id`、`artifact_id`、
`checksum`、`size` 和错误码。租约丢失记录 `app_bootstrap_lease_lost_total`，且旧 Worker 在
租约过期后不得继续调用外部 Driver 或写入 Capture。

权限：

```bash
sudo chown root:alioth /opt/alioth/config/alioth.env
sudo chmod 640 /opt/alioth/config/alioth.env
sudo stat -c '%U %G %a %n' /opt/alioth/config/*.env
```

禁止在排障时执行 `cat /opt/alioth/config/alioth.env` 并把输出复制到聊天或工单。

## 9. 首次手工部署

进入仓库：

```bash
cd /opt/alioth/current
```

定义统一命令，后续示例中的 `$COMPOSE` 均代表同一套生产配置：

```bash
COMPOSE='docker compose --env-file /opt/alioth/config/compose.env -f infra/docker-compose.production.yml'
```

### 9.1 登录 GitLab Registry

推荐创建只具备 `read_registry` 权限的 Deploy Token：

```bash
read -r -p 'Registry user: ' REGISTRY_USER
read -r -s -p 'Registry password: ' REGISTRY_PASSWORD
echo
printf '%s' "$REGISTRY_PASSWORD" | \
  docker login 192.46.230.40:5050 \
    --username "$REGISTRY_USER" \
    --password-stdin
unset REGISTRY_PASSWORD
```

Registry 地址按实际配置替换。

### 9.2 校验 Compose

```bash
$COMPOSE --profile ops config --quiet
$COMPOSE --profile ops config --services
```

预期包含：

```text
alioth-migrate
alioth-initialize
alioth-api
alioth-worker
alioth-workstation
alioth-gateway
```

### 9.3 拉取镜像

```bash
$COMPOSE --profile ops pull
```

记录镜像：

```bash
$COMPOSE images
docker image inspect \
  "$(grep '^CI_REGISTRY_IMAGE=' /opt/alioth/config/compose.env | cut -d= -f2)/api:$(grep '^ALIOTH_IMAGE_TAG=' /opt/alioth/config/compose.env | cut -d= -f2)" \
  --format '{{.Id}} {{.RepoTags}}'
```

### 9.4 数据库备份

任何正式 Migration 前先执行已有备份：

```bash
sudo /opt/data-services/scripts/backup-all.sh
sudo find /opt/data-services/backups -maxdepth 2 -type f -ls
```

确认本次备份文件确实生成后再继续。

### 9.5 执行 Migration

```bash
$COMPOSE --profile ops run --rm alioth-migrate
```

检查 Head：

```bash
$COMPOSE --profile ops run --rm \
  --entrypoint python \
  alioth-migrate \
  -m alembic -c services/api/migrations/alembic.ini current
```

当前预期：

```text
20261023_0146 (head)
```

Migration 失败时立即停止，不得继续启动 API、Worker。

### 9.6 初始化业务数据

```bash
$COMPOSE --profile ops run --rm alioth-initialize
```

该任务依次执行：

```text
seed_first_ruleset.py
→ seed_products.py
→ sync_capability_registry.py
```

三个脚本均应保持幂等，可以在每次部署时重复执行。

当前数据基线：

- 规则集：1 个。
- 首期规则：39 条。
- 基础产品：79 条。
- Capability Registry：以当前镜像中的 Skill/Tool Manifest 为准。

### 9.7 启动常驻服务

```bash
$COMPOSE up -d \
  alioth-api \
  alioth-worker \
  alioth-workstation \
  alioth-gateway
```

检查状态：

```bash
$COMPOSE ps
$COMPOSE logs --tail=200 alioth-api alioth-worker alioth-gateway
```

Worker 日志必须出现：

```text
Alioth Worker 已启动
```

### 9.8 修改默认管理员密码

```bash
read -r -s -p 'New admin password: ' ADMIN_PASSWORD
echo
export ADMIN_PASSWORD

$COMPOSE exec -T \
  -e ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  alioth-api python - <<'PY'
import asyncio
import os

import asyncpg

from services.api.app.api.v1.auth import hash_password


async def main() -> None:
    database_url = os.environ["ALIOTH_DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    connection = await asyncpg.connect(database_url)
    try:
        await connection.execute(
            """
            UPDATE qc_user
            SET password_hash = $1,
                updated_at = now()
            WHERE username = 'admin'
            """,
            hash_password(os.environ["ADMIN_PASSWORD"]),
        )
    finally:
        await connection.close()


asyncio.run(main())
PY

unset ADMIN_PASSWORD
```

## 10. 首次部署验收

### 10.1 容器状态

```bash
$COMPOSE ps
docker inspect alioth-api --format '{{json .State.Health}}'
docker inspect alioth-worker --format '{{.State.Status}} {{.RestartCount}}'
```

### 10.2 HTTP 和 Worker 状态

```bash
curl --fail http://127.0.0.1/api/v1/health
curl --fail http://192.46.230.40/api/v1/health

sudo -u alioth -H bash -lc '
  cd /opt/alioth/current
  infra/scripts/verify-production.sh
'
```

系统状态必须满足：

```text
queue.redis_available = true
worker.alive = true
worker.active_instances >= 1
```

注意：`/api/v1/health` 当前只代表 API 进程存活，不能代替 Worker、PostgreSQL、Redis、MinIO 和真实任务验收。
`/api/v1/workstation/system/status` 属于登录后业务接口，手工调用时必须携带有效的
`Authorization: Bearer <token>`，生产部署脚本不得保存管理员密码或匿名调用该接口。
`verify-production.sh` 会在 API 容器内直接检查 Redis 和 Worker 心跳。

### 10.3 数据基线

使用数据库容器或 PostgreSQL 客户端执行：

```sql
SELECT version_num FROM alembic_version;
SELECT ruleset_code, version, status, snapshot_hash FROM qc_ruleset;
SELECT count(*) AS rule_count FROM qc_rule;
SELECT count(*) AS product_count FROM qc_product;
SELECT capability_kind, status, count(*)
FROM qc_agent_capability_registry
GROUP BY capability_kind, status
ORDER BY capability_kind, status;
```

必须确认：

- Alembic 为当前唯一 Head。
- `alioth_first_ruleset@1` 存在。
- `snapshot_hash` 非空。
- 规则数为 40。
- 产品数为 79。
- Skill 和 Tool 均存在 enabled 能力。

### 10.4 真实 Workstation 任务

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1/api/v1/workstation/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "task_name": "Docker Compose 部署验收任务",
    "target_url": "<TEST_PRODUCT_URL>",
    "ruleset_code": "alioth_first_ruleset",
    "ruleset_version": 1,
    "channel": "workbench",
    "priority": 100,
    "scan_mode": "full",
    "journey_steps": [],
    "remark": "生产 Compose 单机部署验收"
  }'
```

保存 `task_id` 和 `run_id`：

```bash
curl --fail http://127.0.0.1/api/v1/workstation/tasks/<TASK_ID>
curl --fail http://127.0.0.1/api/v1/agents/runs/<RUN_ID>
```

最终必须确认：

1. Redis 中 Run 成功入队并被 Worker 消费。
2. `qc_agent_run` 写入本次 Run。
3. `qc_page_capture` 存在页面采集记录。
4. `qc_evidence_artifact` 存在证据记录。
5. MinIO `alioth` Bucket 出现本任务对象。
6. `qc_rule_execution` 存在规则执行结果。
7. 报告绑定本次 `run_id`。
8. Workstation 能展示任务、页面、规则、证据和报告。

MinIO 验证：

```bash
cd /opt/minio
sudo ./mc.sh ls --recursive local/alioth/<TASK_ID>/
```

## 11. 手工发布流程

生产部署文件落地后，推荐直接调用仓库脚本，脚本会按“镜像拉取 → 备份 → 停服务 → Migration → 初始化 → 启动 → 基础验收”的顺序执行：

```bash
cd /opt/alioth/current
infra/scripts/deploy-production.sh <DEPLOY_COMMIT_SHA>
```

### 11.1 当前服务器本地构建重部署

GitLab Container Registry 尚未启用、服务器使用 `alioth-local/*` 镜像时，不得调用会执行 `compose pull` 的 `deploy-production.sh`。代码合并到 `dev` 后，流水线使用触发本次部署的完整 Commit SHA 调用：

```bash
sudo -u alioth -H \
  /opt/alioth/current/infra/scripts/redeploy-local-production.sh \
  --branch dev \
  --commit <FULL_COMMIT_SHA>
```

脚本固定执行以下步骤：

```text
检查部署目录和发布锁
→ 同步 origin/dev 并校验待部署 Commit 属于该分支
→ 使用新 Commit SHA 构建 api/workstation/admin/gateway 四类镜像
→ 执行 PostgreSQL、Redis 发布前备份
→ 更新 compose.env 的 ALIOTH_IMAGE_TAG
→ 停止 Gateway、Worker、API
→ 执行 Migration
→ 初始化规则、产品和 Capability Registry
→ 以同一 Commit SHA 重建五个常驻容器
→ 执行基础部署验收
→ 写入 deployment-history.log
```

部署用户必须具备仅执行备份脚本的免密 sudo 权限：

```sudoers
alioth ALL=(root) NOPASSWD: /opt/data-services/scripts/backup-all.sh
```

脚本不会自动执行 Alembic downgrade，也不会把基础健康检查当作真实质检验收。任一步骤失败时会记录失败阶段、保留上一版镜像并输出回滚命令，由发布人确认数据库兼容性后决定是否回滚应用。

可选参数：

```bash
# 当前 Commit 未变化时强制重建
ALIOTH_FORCE_REDEPLOY=1 \
  infra/scripts/redeploy-local-production.sh --branch dev --commit <FULL_COMMIT_SHA>

# 同时拉取 Docker 基础镜像更新
ALIOTH_BUILD_PULL=1 \
  infra/scripts/redeploy-local-production.sh --branch dev --commit <FULL_COMMIT_SHA>
```

下述分步命令用于首次部署、人工审查和故障定位，不应另行维护第二套发布顺序。

每次发布记录旧版本：

```bash
cd /opt/alioth/current
OLD_TAG=$(grep '^ALIOTH_IMAGE_TAG=' /opt/alioth/config/compose.env | cut -d= -f2)
echo "$(date -Is) old=$OLD_TAG" | sudo tee -a /opt/alioth/releases/deployment-history.log
```

更新部署声明：

```bash
sudo -u alioth -H bash -lc '
  cd /opt/alioth/current
  git fetch origin +refs/heads/dev:refs/remotes/origin/dev
  git checkout --detach origin/dev
'
```

将 `ALIOTH_IMAGE_TAG` 更新为待发布 Commit SHA，然后执行：

```bash
$COMPOSE --profile ops config --quiet
$COMPOSE --profile ops pull
sudo /opt/data-services/scripts/backup-all.sh
```

停止业务入口，避免不兼容 Migration 期间继续写入：

```bash
$COMPOSE stop alioth-gateway alioth-worker alioth-api
```

执行迁移和初始化：

```bash
$COMPOSE --profile ops run --rm alioth-migrate
$COMPOSE --profile ops run --rm alioth-initialize
```

启动新版本：

```bash
$COMPOSE up -d \
  alioth-api \
  alioth-worker \
  alioth-workstation \
  alioth-gateway
```

执行健康检查和真实任务验收。验收成功后记录：

```bash
NEW_TAG=$(grep '^ALIOTH_IMAGE_TAG=' /opt/alioth/config/compose.env | cut -d= -f2)
echo "$(date -Is) deployed=$NEW_TAG status=verified" | \
  sudo tee -a /opt/alioth/releases/deployment-history.log
```

## 12. 镜像回滚

回滚不重新构建镜像，只切换到上一个已验收 Commit SHA。

```bash
sudo editor /opt/alioth/config/compose.env
```

修改：

```dotenv
ALIOTH_IMAGE_TAG=<LAST_GOOD_COMMIT_SHA>
```

执行：

```bash
$COMPOSE pull
$COMPOSE stop alioth-gateway alioth-worker alioth-api
$COMPOSE up -d \
  alioth-api \
  alioth-worker \
  alioth-workstation \
  alioth-gateway
```

数据库回滚原则：

- 默认不执行 Alembic downgrade。
- 应用回滚版本必须兼容当前数据库结构。
- 破坏性 Migration 发布前必须设计双版本兼容窗口。
- 确需数据库恢复时，先停止全部应用容器，再使用发布前备份处理。

## 13. 日志和排障

```bash
$COMPOSE logs --tail=300 alioth-api
$COMPOSE logs --tail=300 alioth-worker
$COMPOSE logs --tail=300 alioth-gateway
$COMPOSE logs -f alioth-api alioth-worker
```

检查重启次数：

```bash
docker inspect alioth-api alioth-worker \
  --format '{{.Name}} status={{.State.Status}} restart={{.RestartCount}} error={{.State.Error}}'
```

Worker 反复退出时优先检查：

- LLM Key、Model、Base URL。
- PostgreSQL、Redis、MinIO Docker DNS。
- Redis 密码 URL 编码。
- Alembic Head。
- Rule Engine 启动校验。
- `/ms-playwright` 中的 Chromium。

API health 正常但任务不执行时检查：

```bash
curl -H 'Authorization: Bearer <token>' \
  http://127.0.0.1/api/v1/workstation/system/status
$COMPOSE ps alioth-worker
$COMPOSE logs --tail=300 alioth-worker
```

## 14. GitLab 基础准备

### 14.1 Container Registry

确认 GitLab 项目已启用 Container Registry，并获得实际地址：

```text
<registry-host>/<group>/alioth
```

从部署服务器验证 Registry API。按实际协议和地址执行，未登录时返回 `401 Unauthorized`
或已放行匿名访问时返回 `200 OK` 均表示 Registry 服务正常；TCP 端口可连接但返回空响应
不属于可用状态。

```bash
curl --connect-timeout 5 --max-time 10 -i https://<registry-host>/v2/
```

如果使用内部 CA，优先把 CA 证书安装到 Docker 信任目录，不要长期使用跳过 TLS 校验或
把 Registry 配置为不安全仓库。

需要发布四类镜像：

```text
api:<commit-sha>
workstation:<commit-sha>
admin:<commit-sha>
gateway:<commit-sha>
```

禁止只发布 `latest`。可以额外维护环境标签，但部署必须使用不可变 Commit SHA：

```text
api:dev
api:<commit-sha>
```

### 14.2 GitLab Runner

推荐配置两个执行能力：

| Runner | 用途 | 权限 |
| --- | --- | --- |
| Build Runner | 测试、构建、推送镜像 | Docker Build/Push，不可登录生产服务器 |
| Deploy Runner 或 SSH Job | 拉镜像、Migration、部署、验收 | 仅 Protected Branch/Environment 可用 |

首期可以通过 CI Job 使用 SSH 登录部署服务器。稳定后建议使用受保护的专用 Deploy Runner。

### 14.3 CI/CD Variables

在 GitLab 项目设置中配置：

| 变量 | 类型 | 要求 |
| --- | --- | --- |
| `DEPLOY_HOST` | Variable | `192.46.230.40` |
| `DEPLOY_PORT` | Variable | `22` |
| `DEPLOY_USER` | Variable | 专用部署用户 |
| `DEPLOY_SSH_PRIVATE_KEY` | File | Protected，CI 到部署服务器的专用私钥 |
| `DEPLOY_SSH_KNOWN_HOSTS` | File | Protected，固定服务器 Host Key |

LLM、PostgreSQL、Redis、MinIO 密钥建议只保存在服务器 `/opt/alioth/config/alioth.env`。除非流水线需要动态生成该文件，否则不要把这些业务密钥重复保存进 GitLab。

部署用户如需在流水线执行数据库备份，只允许免密调用这一条固定脚本。创建 `/etc/sudoers.d/alioth-deploy-backup`：

```sudoers
alioth ALL=(root) NOPASSWD: /opt/data-services/scripts/backup-all.sh
```

同时必须确保脚本及其父目录由 root 管理，部署用户不可修改：

```bash
sudo chown root:root /opt/data-services/scripts/backup-all.sh
sudo chmod 750 /opt/data-services/scripts/backup-all.sh
sudo visudo -cf /etc/sudoers.d/alioth-deploy-backup
```

### 14.4 分支和环境保护

- `dev` 设置为 Protected Branch。
- `dev` 部署环境设置为 Protected Environment。
- Merge Request 流水线只运行测试和构建，不允许自动部署。
- 代码合并进入 `dev` 后，Push Pipeline 在测试通过时自动部署。
- 部署使用 `resource_group` 和服务器文件锁禁止并发发布。

## 15. GitLab CI/CD 流程

推荐阶段：

```yaml
stages:
  - lint
  - test
  - publish
  - deploy
  - verify
```

目标流程：

```text
Ruff
→ Pytest
→ Workstation npm ci/build
→ Admin npm ci/build
→ SSH 到部署服务器
→ 校验 CI_COMMIT_SHA 属于 origin/dev
→ 在服务器构建四个 Commit SHA 镜像
→ 服务器备份
→ Migration
→ 初始化规则/产品/Capability
→ Compose Up
→ HTTP/Worker 验收
→ 真实 Workstation 任务验收
```

### 15.1 测试任务示例

```yaml
backend-lint:
  stage: lint
  image: python:3.12-slim
  script:
    - pip install --no-cache-dir -e ".[dev]"
    - python -m ruff check services scripts

backend-test:
  stage: test
  image: python:3.12-slim
  script:
    - pip install --no-cache-dir -e ".[dev]"
    - python -m playwright install --with-deps chromium
    - python -m pytest services/api/tests -q

workstation-build:
  stage: test
  image: node:22-bookworm-slim
  script:
    - cd app/workstation
    - npm ci
    - npm run build
```

### 15.2 Registry 镜像发布

当前服务器使用 `alioth-local/*:<commit-sha>`，由部署脚本在服务器本地构建镜像，因此 `dev` Pipeline 不执行 Docker-in-Docker 和 Registry Push。保留的 `docker-images` Job 只有在后续启用 GitLab Container Registry 并显式设置 `ALIOTH_REGISTRY_PUBLISH_ENABLED=1` 时才运行：

```yaml
docker-images:
  stage: publish
  image: docker:27-cli
  services:
    - name: docker:27-dind
  variables:
    DOCKER_HOST: "tcp://docker:2376"
    DOCKER_TLS_CERTDIR: "/certs"
    DOCKER_TLS_VERIFY: "1"
    DOCKER_CERT_PATH: "/certs/client"
  before_script:
    - echo "$CI_REGISTRY_PASSWORD" | docker login "$CI_REGISTRY" -u "$CI_REGISTRY_USER" --password-stdin
  script:
    - docker build -f infra/docker/api/Dockerfile.production -t "$CI_REGISTRY_IMAGE/api:$CI_COMMIT_SHA" .
    - docker build -f infra/docker/workstation/Dockerfile -t "$CI_REGISTRY_IMAGE/workstation:$CI_COMMIT_SHA" .
    - docker build -f infra/docker/admin/Dockerfile -t "$CI_REGISTRY_IMAGE/admin:$CI_COMMIT_SHA" .
    - docker build -f infra/docker/gateway/Dockerfile -t "$CI_REGISTRY_IMAGE/gateway:$CI_COMMIT_SHA" .
    - docker push "$CI_REGISTRY_IMAGE/api:$CI_COMMIT_SHA"
    - docker push "$CI_REGISTRY_IMAGE/workstation:$CI_COMMIT_SHA"
    - docker push "$CI_REGISTRY_IMAGE/admin:$CI_COMMIT_SHA"
    - docker push "$CI_REGISTRY_IMAGE/gateway:$CI_COMMIT_SHA"
  rules:
    - if: '$CI_COMMIT_TAG && $ALIOTH_REGISTRY_PUBLISH_ENABLED == "1"'
```

### 15.3 部署任务示例

```yaml
deploy-acceptance:
  stage: deploy
  image: alpine:3.21
  resource_group: alioth-dev
  environment:
    name: dev
    url: http://192.46.230.40/
  before_script:
    - apk add --no-cache openssh-client
    - install -m 700 -d ~/.ssh
    - cp "$DEPLOY_SSH_PRIVATE_KEY" ~/.ssh/id_ed25519
    - chmod 600 ~/.ssh/id_ed25519
    - cp "$DEPLOY_SSH_KNOWN_HOSTS" ~/.ssh/known_hosts
  script:
    - |
      ssh -p "$DEPLOY_PORT" "$DEPLOY_USER@$DEPLOY_HOST"
      "cd /opt/alioth/current &&
      git fetch origin
      '+refs/heads/dev:refs/remotes/origin/dev' &&
      git checkout --detach '$CI_COMMIT_SHA' &&
      infra/scripts/redeploy-local-production.sh
      --branch dev
      --commit '$CI_COMMIT_SHA'"
  rules:
    - if: '$CI_COMMIT_BRANCH == "dev" && $CI_PIPELINE_SOURCE == "push"'
      when: on_success
      allow_failure: false
```

部署任务必须使用 `resource_group` 防止同一环境并发发布。

### 15.4 真实链路验收

HTTP health 只能作为基础检查。建议增加受保护的 `verify-e2e` Job：

- 创建正式 Workstation 任务。
- 轮询 `run_id` 直到完成或超时。
- 检查任务状态不是永久 queued/running。
- 查询 PostgreSQL 中的 Run、页面、证据和规则执行。
- 检查 MinIO 对象存在。
- 检查最终报告绑定本次 Run。

真实质检会调用 LLM 并产生费用，默认设置为手工执行，不能在每个普通 Commit 自动触发。

## 16. 流水线失败处理

`backend-lint` 已作为阻断门禁执行 `python -m ruff check services scripts`。历史 Airtest 参考脚本与一次性现场诊断脚本通过 `pyproject.toml` 明确排除；少量历史设备适配、OpenAPI/HTML 模板和兼容性测试仅按文件隔离 E501 长行，其余 Ruff 规则仍为强制门禁。

| 阶段 | 失败处理 |
| --- | --- |
| Ruff | 禁止合并或发布；只允许 `pyproject.toml` 中声明的历史/生成资产豁免 |
| Test/前端 Build/Compose 校验 | 禁止合并或发布 |
| 前端 Build | 禁止构建镜像 |
| Docker Build | 不更新部署标签 |
| Registry Push | 不进入 Deploy |
| Backup | 不执行 Migration |
| Migration | 不启动新版本 |
| Initialize | 不启动 Worker |
| Health | 自动查看容器日志，执行镜像回滚 |
| E2E | 保留新版本现场，人工判断回滚或修复 |

任何阶段都不能为了“让流水线变绿”而跳过 Migration、Capability 同步或真实链路验收。

## 17. 备份与恢复

PostgreSQL、Redis 使用 Docker Volume 持久化，不等于已有离线备份。每次发布前必须执行：

```bash
sudo /opt/data-services/scripts/backup-all.sh
sudo find /opt/data-services/backups -maxdepth 2 -type f -ls
```

同时要求：

- PostgreSQL 定期逻辑备份并验证恢复。
- Redis RDB/AOF 定期导出并验证。
- MinIO 开启 Versioning，并制定证据保留和清理策略。
- `/opt/alioth/config/*.env` 使用安全渠道备份，不进入代码备份包。
- 每月至少执行一次恢复演练。

## 18. 网络安全

同机部署后推荐端口边界：

| 端口 | 策略 |
| --- | --- |
| `22` | 仅允许运维来源 IP |
| `80/443` | 工作台入口 |
| `8088` | 仅允许办公网、VPN 或堡垒机 |
| `8000` | 不映射到宿主机 |
| `5432` | 不公开，应用通过 `data_services_net` 访问 |
| `6379` | 不公开，应用通过 `data_services_net` 访问 |
| `9000` | 不公开，应用通过 `minio_net` 访问 |
| `9001` | 仅允许运维来源 IP |

如果中间件仍有 `0.0.0.0:5432/6379/9000` 的宿主机映射，应在确认无外部调用方后改为回环绑定或移除端口映射。Docker 发布端口可能绕过部分 UFW 规则，仅依赖 UFW deny 不够可靠。

## 19. systemd 备选方案

只有在以下情况才考虑回退到 systemd：

- GitLab Registry 暂时无法提供。
- Docker 构建链路长期不可用。
- 服务器运维标准明确禁止应用容器化。

systemd 方案需要自行维护 Python venv、Playwright、Node 构建产物、Nginx、依赖升级和发布回滚，部署一致性明显弱于 Docker Compose，因此不作为默认方案。

## 20. 上线检查清单

- [ ] 生产 Dockerfile、Compose、Nginx 和 `.dockerignore` 已进入仓库。
- [ ] `.gitlab-ci.yml` 已配置测试、镜像构建、部署和验收阶段。
- [ ] GitLab Container Registry 未启用时，服务器使用 `alioth-local/*:<commit-sha>` 本地镜像。
- [ ] Build Runner 和 Deploy 权限已隔离。
- [ ] `dev` 和部署环境已受保护。
- [ ] 部署使用 Commit SHA 镜像，不只使用 `latest`。
- [ ] PostgreSQL、Redis、MinIO 只通过外部 Docker 网络访问。
- [ ] 真实密码和 Key 未进入 Git、镜像和日志。
- [ ] Alembic 只有一个 Head，并已升级到当前 Head。
- [ ] 规则集、产品、Capability Registry 已同步。
- [ ] 默认管理员密码已修改。
- [ ] API、Worker、前端和 Gateway 容器状态正常。
- [ ] Worker 心跳正常。
- [ ] PostgreSQL、Redis 备份已实际生成。
- [ ] MinIO Bucket、权限和 Versioning 已确认。
- [ ] 真实 Workstation 任务端到端验收通过。
- [ ] PostgreSQL 运行记录、MinIO 证据对象和报告绑定已核对。
- [ ] 上一版 Commit SHA 和镜像仍可用于回滚。
- [ ] 鉴权加固完成前，管理后台和 API 未直接暴露公网。
