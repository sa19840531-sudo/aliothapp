# H5 五站代码审查与真实复跑执行计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 审查 `codex/h5-five-site-exploration-fixes-20260901` 相对 `origin/dev_1.5` 的 H5 探索实现，在不构建镜像的前提下重启本地 Docker，自动登录后重新探索用户指定的五个站点，并输出带页面类型、截图和运行证据的 HTML 报告。

**Architecture:** 静态审查以 `git diff origin/dev_1.5...HEAD` 为唯一分支差异口径；运行验证使用 `infra/docker-compose.yml` 的挂载代码容器，以 API 创建任务、worker 顺序消费、PostgreSQL/HTTP 查询收集证据。最终报告只把实际 Capture/判定事实计入业务页型覆盖，并把任务期望、动作意图和真实落点分开呈现。

**Tech Stack:** Python 3、FastAPI、PostgreSQL/pgvector、Redis、Docker Compose、PowerShell、HTML5/CSS。

**Spec:** 本计划直接实现当前用户请求；任务请求体以用户提供字段为基准，仅按站点替换 `task_name` 与 `target.url`。

## Global Constraints

- 当前分支必须是 `codex/h5-five-site-exploration-fixes-20260901`，审查基线固定为 `origin/dev_1.5`。
- Docker 只允许 `docker compose ... up -d --no-build --force-recreate` 或 `restart`，禁止任何 `build`/`--build`。
- 登录账号为 `admin`；密码与 token 只保存在当前 PowerShell 进程变量中，不写入报告、日志或结果文件。
- 五个站点按用户给出的顺序提交，`scan_mode=full`、`ruleset_code=alioth_first_ruleset`、`ruleset_version=1`、`channel=workbench`、`priority=100`。
- 报告中的业务页型覆盖必须来自实际 Capture/页面类型判定，不得用 `expected_next_page_type`、目标动作或默认流程冒充实际识别。
- 所有新增文档使用 UTF-8，最终产物放在 `docs/reports`，截图引用使用仓库内可解析路径或受鉴权 API 路径。

---

### Task 1: 静态代码审查

**Files:**
- Review: `services/api/app/agents/tools/page_detection_tools.py`
- Review: `services/api/app/application/h5_exploration_*.py`
- Review: `services/api/app/application/h5_*_navigation.py`
- Review: `services/api/app/domain/exploration/h5_*.py`
- Review: `services/api/app/infrastructure/browser/*.py`
- Review: `services/api/tests/test_h5_*.py`
- Review: `services/api/tests/test_page_*.py`
- Review: `services/api/tests/test_web_capture_runtime.py`

- [ ] **Step 1:** 记录 `git status --short --branch`、`git merge-base HEAD origin/dev_1.5` 与 `git diff --name-status origin/dev_1.5...HEAD`。
- [ ] **Step 2:** 按正确性、状态一致性、导航安全、证据真实性、异常恢复、审计完整性逐文件审查生产代码差异。
- [ ] **Step 3:** 对每条可疑路径追到调用方和现有测试，只有能给出文件/行号、触发条件与影响时才写入最终 findings。
- [ ] **Step 4:** 运行分支新增/修改的 H5 定向测试，记录通过数、失败数和耗时；失败时先使用 `superpowers:systematic-debugging` 定位根因。

### Task 2: Docker 无构建重启与健康验证

**Files:**
- Read: `infra/docker-compose.yml`
- Read: `infra/env/.env.local`

- [ ] **Step 1:** 用 `docker compose -f infra/docker-compose.yml ps` 和 `docker inspect` 记录重启前容器状态、镜像 ID 与启动时间。
- [ ] **Step 2:** 执行 `docker compose -f infra/docker-compose.yml up -d --no-build --force-recreate alioth-postgres alioth-redis alioth-api alioth-worker`。
- [ ] **Step 3:** 等待 PostgreSQL/Redis 健康、API 监听 8000、worker 无启动错误；再次记录镜像 ID，证明没有 build/换镜像。

### Task 3: 鉴权并创建五站任务

**Interfaces:**
- Consumes: `POST http://127.0.0.1:8000/api/v1/auth/login`
- Produces: 五个 `POST http://127.0.0.1:8000/api/v2/tasks` 响应中的 task ID。

- [ ] **Step 1:** 在单个 PowerShell 进程内登录并从响应 `token` 字段生成 Bearer 头，不输出 token。
- [ ] **Step 2:** 按顺序提交五个任务，URL 依次为 `https://dxbbmcw6gltgg.cloudfront.net/?devTest=123`、`https://dvgsyzqus02v5.cloudfront.net/?devTest=1`、`http://d24jdtq5qz8ff4.cloudfront.net`、`http://d3jsxm6ahe66k1.cloudfront.net`、`https://d8zz39epttv3v.cloudfront.net/?/?devTest=123`。
- [ ] **Step 3:** 保存不含凭据的任务 ID、创建时间、目标 URL 与原始创建响应到临时证据文件。

### Task 4: 跟踪运行并收集探索证据

**Interfaces:**
- Consumes: 五个 task ID、任务详情/进度/Run/Capture/结果查询接口与只读数据库查询。
- Produces: 每站时间线、终态、Run、Capture、实际页型、动作、阻塞原因、截图与用量审计汇总。

- [ ] **Step 1:** 周期性查询五个任务，记录每次状态变化、当前阶段、进度、最近错误和更新时间；最长等待以 worker 仍有进展为准。
- [ ] **Step 2:** 对每个任务收集 agent run、command/step、capture、page type assessment、completeness、rule result、LLM usage 与截图元数据。
- [ ] **Step 3:** 将截图复制或导出到 `docs/reports/assets/2026-09-01-h5-five-site-review-rerun/`，不得写入 token；无法导出时记录受鉴权资源路径与失败原因。
- [ ] **Step 4:** 交叉核对动作期望、Capture 真实 URL、识别页型和任务终态，标注误判、重复探索、无进展、超时、弹窗/广告阻断及站点不可达。

### Task 5: 生成并验证 HTML 报告

**Files:**
- Create: `docs/reports/2026-09-01-h5-five-site-review-rerun-report.html`

- [ ] **Step 1:** 报告包含环境/分支、无构建重启证据、鉴权与任务请求摘要、静态审查 findings、五站总览、逐站时间线、真实页型矩阵、截图画廊、任务/Run 终态、日志/错误、用量和结论。
- [ ] **Step 2:** 对所有本地截图引用执行存在性检查，对 HTML 做 UTF-8 读取、标签/表格基本完整性检查，并扫描凭据泄漏。
- [ ] **Step 3:** 重新查询五个任务终态和 Docker 状态，以新鲜证据更新报告末尾的验证时间。
- [ ] **Step 4:** 执行 `git diff --check`，确认只新增本计划、最终报告和必要的报告资源，没有改动业务代码或用户既有文件。
