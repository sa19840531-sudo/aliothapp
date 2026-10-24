# dvgsyz H5 完整探索验证 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不重新构建镜像的前提下，对 `https://dvgsyzqus02v5.cloudfront.net/?devTest=1` 完成 H5 V2 全链路探索，验证向量知识、Worker 执行与约 90% 适用页型覆盖，并修复真实阻断。

**Architecture:** 以本地 FastAPI V2 任务入口创建完整扫描任务，以 PostgreSQL 中的 Capture、页型评估、Action Execution 和 V2 Work Item 作为事实源。只有运行证据证明探索被平台缺陷阻断时，才进入系统化调试和测试驱动修复；修复后只重启受影响 Docker 服务，不执行 build，并创建新任务复验。

**Tech Stack:** PowerShell、Git、Docker Compose、FastAPI、PostgreSQL、pytest、Ruff

**Spec:** `docs/专项能力与高频回归规则.md`

## Global Constraints

- 分支固定为 `dev_1.5`，同步远程时禁止覆盖未提交用户改动。
- Docker 只能 restart/recreate，禁止 `build` 和 `--build`。
- 登录账号使用用户提供的本地测试账号，密码只在执行进程内读取；token 不写入仓库和最终报告。
- 页面类型目标为 `line_loading/startup_ad/home/content_list/content_detail/video_list/video_detail/ranking/profile/settings/payment/post_list/activity`。
- 覆盖率同时报告原始 13 类比例和排除目标站点无入口类型后的适用比例。
- 代码修复必须先有可复现失败测试，所有新增注释使用中文 UTF-8，并同步专项回归规则。
- 本次执行不自动提交或推送 Git；除非用户另行授权。

---

### Task 1: 同步分支并确认运行基线

**Files:**
- Inspect: `.git`
- Inspect: `infra/docker-compose.yml`

**Interfaces:**
- Consumes: 远程 `origin/dev_1.5`
- Produces: 与远程一致且无冲突的本地工作树

- [ ] **Step 1: 读取当前分支、状态和远程差异**

Run: `git status --short --branch`、`git rev-list --left-right --count HEAD...origin/dev_1.5`

- [ ] **Step 2: 获取并整合远程提交**

Run: `git fetch origin dev_1.5`，仅在工作区安全时执行 `git pull --ff-only origin dev_1.5`。

- [ ] **Step 3: 核对 HEAD**

Run: `git rev-parse HEAD` 与 `git rev-parse origin/dev_1.5`；预期输出相同。

### Task 2: 无构建重启与鉴权

**Files:**
- Inspect: `infra/docker-compose.yml`
- Inspect: `infra/env/.env.local`

**Interfaces:**
- Consumes: 已存在的 `alioth-api:local` 镜像和 Compose 服务
- Produces: 可用的 API/Worker 与仅驻留内存的 bearer token

- [ ] **Step 1: 无 build 重启 Compose 服务**

Run: `docker compose -f infra/docker-compose.yml restart`；命令中不得出现 `build`。

- [ ] **Step 2: 验证容器与 readiness**

Run: `docker ps`、`GET http://127.0.0.1:8000/health/ready`，确认 API、Worker、PostgreSQL、Redis 正常。

- [ ] **Step 3: 登录并仅在进程内保存 token**

向仓库实际登录端点提交用户提供的本地测试凭据，从响应读取 access token；禁止在计划、日志或最终报告中回显凭据与完整 token。

### Task 3: 创建单站完整扫描任务

**Files:**
- No file changes.

**Interfaces:**
- Consumes: bearer token、`POST /api/v2/tasks`
- Produces: 新任务 ID

- [ ] **Step 1: 提交固定任务合同**

```json
{
  "task_name": "测试1-dvgsyz",
  "target": {
    "platform": "h5",
    "url": "https://dvgsyzqus02v5.cloudfront.net/?devTest=1"
  },
  "ruleset_code": "alioth_first_ruleset",
  "ruleset_version": 1,
  "channel": "workbench",
  "priority": 100,
  "product": {
    "department_name": "本地测试",
    "product_code": "LOCAL-E2E",
    "product_name": "本地完整链路测试"
  },
  "scan_mode": "full",
  "journey_steps": [],
  "remark": "H5 V2 完整链路测试"
}
```

- [ ] **Step 2: 保存任务 ID 并确认 Work 已发布**

查询任务详情与 `qc_v2_work_item`，预期至少出现 exploration 工作项或明确的受控终态。

### Task 4: 审计向量应用、Worker 和覆盖

**Files:**
- No file changes.

**Interfaces:**
- Consumes: `qc_page_capture.metadata.page_detection.navigation_knowledge`、`qc_h5_page_type_assessment`、`qc_action_execution`、`qc_v2_work_item`
- Produces: 向量检索统计、动作结果、探索页型清单和覆盖率

- [ ] **Step 1: 持续读取 Worker 终态**

按任务 ID 聚合 Work Item 与 Action Execution 状态；只在 exploration 不再有 `available/leased/running` 后评估覆盖。

- [ ] **Step 2: 核对向量知识元数据**

汇总 `repository_configured/repository_degraded/exact_lookup_attempt_count/vector_lookup_attempt_count/match_type_counts`，区分“已调用但无命中”和“仓库未配置”。

- [ ] **Step 3: 列出实际页面类型**

仅使用已落库 assessment 的 `page_type`，不把候选 `expected_next_page_type` 当成覆盖事实。

- [ ] **Step 4: 计算覆盖率**

原始覆盖率为实际命中目标类型数除以 13；适用覆盖率排除已证明站点无入口或不可达的类型，并记录证据。

### Task 5: 阻断修复与复验

**Files:**
- Modify only after root cause identifies the owning module.
- Test: reuse the matching `services/api/tests/test_h5_*.py` or browser runtime test file.
- Modify: `docs/专项能力与高频回归规则.md` for reusable regressions.

**Interfaces:**
- Consumes: 失败 Action 的 before Capture、错误码、Snapshot/DOM 诊断和 Worker 日志
- Produces: 最小生产修复、失败先行的回归测试和新任务复验证据

- [ ] **Step 1: 证明阻断而非站点无入口**

同一候选必须有可达结构证据，并出现平台错误、错误目标、恢复失败或可重复的 Worker 异常；单纯未发现入口不授权放宽点击边界。

- [ ] **Step 2: 写最小失败测试并验证 RED**

用脱敏后的真实 DOM/Snapshot/Action 事实构造单一行为测试；预期失败原因必须与线上阻断一致。

- [ ] **Step 3: 实现单一根因修复并验证 GREEN**

只修改阻断所属边界，不增加 force click、任意文本点击、坐标盲点或高风险操作。

- [ ] **Step 4: 运行回归与静态检查**

Run: 目标测试、H5 全套测试、完整 `services/api/tests`、变更 Python 文件 Ruff 和 `git diff --check`。

- [ ] **Step 5: 不 build 重启受影响服务并创建新任务复验**

使用现有镜像/源码挂载重启 API/Worker，重新登录并创建新的完整扫描任务；旧任务证据不得冒充修复后结果。
