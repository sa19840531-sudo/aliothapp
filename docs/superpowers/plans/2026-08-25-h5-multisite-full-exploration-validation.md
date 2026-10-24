# H5 多站完整探索验证 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不构建 Docker 镜像的前提下，为 26 个 H5 站点分别执行 V2 完整扫描，审计向量导航知识、Worker 行为、实际探索页面和约 90% 的适用页型覆盖，并修复可复现的平台阻断。

**Architecture:** 每个网址独立创建一个 `scan_mode=full` 任务，使用 PostgreSQL 中的 Capture、页型评估、Action Execution 和 V2 Work Item 作为事实源。覆盖率只统计已落库的实际页型；向量审计从 `qc_page_capture.metadata.page_detection.navigation_knowledge` 读取。只有证据证明平台阻断时才进入系统化调试和 TDD 修复，修复后只重启已有源码挂载容器并创建新任务复验。

**Tech Stack:** PowerShell、Docker Compose、FastAPI、PostgreSQL/pgvector、Redis、pytest、Ruff

**Spec:** `docs/专项能力与高频回归规则.md`

## Global Constraints

- Docker 只允许 `restart`，禁止 `build`、`--build` 和镜像重建。
- 保留当前工作区全部未提交改动，不回退、不覆盖、不自动提交。
- 凭据与 bearer token 只驻留执行进程内，不写文件、不回显。
- 每个网址单独创建任务，首轮任务名称严格使用用户给定的 `测试1`；修复后复验任务使用可审计的复验名称，其余合同字段保持用户给定值。
- 无 scheme 的网址补为 `https://`；已显式声明 `http://` 的网址保持 HTTP。
- 用户关注页型为 `line_loading/startup_ad/home/content_list/content_detail/video_list/video_detail/ranking/profile/settings/payment/post_list/activity`；覆盖分母以版本化页型 Catalog 为准，Catalog 已明确移除的 `ranking` 单列为不适用，不得为凑覆盖率重新加入。
- 原始展示同时保留用户清单 13 项命中数与 Catalog 适用项命中数；约 90% 的验收以实际落库证据和适用分母计算，外部站点不可达、验证码等阻断必须单列，不能冒充已覆盖。
- 候选动作的 `expected_next_page_type` 不计入实际覆盖。
- 新阻断修复必须先复现并写失败测试，新增注释使用中文 UTF-8；有复用价值时同步更新 `docs/专项能力与高频回归规则.md`。

---

### Task 1: 建立运行基线并无构建重启

**Files:**
- Inspect: `infra/docker-compose.yml`
- Inspect: current Git worktree

- [ ] **Step 1: 记录 Git 状态、容器镜像 ID 与启动时间**
- [ ] **Step 2: 执行 `docker compose -f infra/docker-compose.yml restart`**
- [ ] **Step 3: 再次核对镜像 ID 未变化，并验证 PostgreSQL、Redis、API、Worker 健康**

### Task 2: 登录并创建 26 个独立完整扫描任务

**Files:** No source edits.

- [ ] **Step 1: 调用 `POST /api/v1/auth/login`，只在当前 PowerShell 进程保存 token**
- [ ] **Step 2: 按下列顺序规范化目标并逐个调用 `POST /api/v2/tasks`**

```text
01 https://51cg.app
02 https://mrds1.app
03 https://sawd.medrh0j3.com
04 https://nvhh.fousmj7e.com
05 https://h5.ridlkncrjs.com/
06 https://rd.mhhyj29j.top
07 https://xv.xta533.com
08 https://bz1219.w83pnbmr.work/
09 https://d3ps07dw2j7cjj.cloudfront.net/?devTest=123
10 https://kezvhj.08oai46.work/
11 https://35.229.238.103:2018/
12 https://91ab.me/
13 https://haijiaoq.com
14 https://ttav.life/
15 https://51tkpj1.com
16 https://phob.cc
17 https://91bbs.net
18 http://p5.cyxnzwms.com
19 https://dy.ft6g7b8pfiy.cc/
20 https://0mst.nmxtsvw0.com
21 https://txh068.com/?checkin=babadebabashiyeye
22 http://d1upogjpc3aj7g.cloudfront.net
23 https://d4kclpuyztc4c.cloudfront.net
24 https://da8ttmrfqqhqf.cloudfront.net/?devTest=123
25 https://d2m7hqfixhkpkl.cloudfront.net/
26 https://dsq3p4z6gl5ag.cloudfront.net/?devTest=1123
```

- [ ] **Step 3: 仅记录序号、目标、task_id、run_id 和初始状态，不记录 token**

### Task 3: 持续观察 Worker 与向量应用

**Files:** No source edits.

- [ ] **Step 1: 按任务聚合 `qc_v2_work_item`，直到 exploration 不再存在 `available/leased/running`**
- [ ] **Step 2: 汇总 Action Execution 的执行、失败、阻断、错误码和导航深度**
- [ ] **Step 3: 从 `navigation_knowledge` 汇总 repository configured/degraded、exact/vector 尝试次数、命中类型与是否写入候选 metadata**
- [ ] **Step 4: 分别报告“向量已调用但零命中”和“向量仓库未应用”**

### Task 4: 计算单站与集合覆盖

**Files:** No source edits.

- [ ] **Step 1: 列出每个任务落库 Capture 的 URL、page_seq、page_type、page_state、capture_id 与对应动作路径**
- [ ] **Step 2: 用 assessment 实际页型对 13 类逐站打勾，计算单站原始覆盖率与适用覆盖率**
- [ ] **Step 3: 计算 26 站集合并集覆盖，区分目标站无入口、外部站点不可达和平台探索缺陷**

### Task 5: 对真实阻断执行 TDD 修复

**Files:**
- Test first: 与失败边界对应的 `services/api/tests/test_*.py`
- Modify after RED: 失败边界所属的单一生产文件
- Modify when reusable: `docs/专项能力与高频回归规则.md`

- [ ] **Step 1: 保存可重复的错误码、before/after Capture、DOM/Snapshot、Work Item 和 Worker 日志证据**
- [ ] **Step 2: 用脱敏证据写一个最小失败测试并确认 RED 原因一致**
- [ ] **Step 3: 实现单一根因修复并确认目标测试 GREEN**
- [ ] **Step 4: 运行相邻 H5 回归、完整 API 测试、Ruff 与 `git diff --check`**
- [ ] **Step 5: 仅 restart API/Worker，创建新任务复验；旧任务不得作为修复后证据**

### Task 6: 最终验收

**Files:** No additional source edits.

- [ ] **Step 1: 对所有任务执行一次新鲜数据库汇总，核对终态、页型、动作和向量证据**
- [ ] **Step 2: 对所有代码改动执行目标测试、相邻回归、Ruff 和 `git diff --check`**
- [ ] **Step 3: 报告 Docker 镜像 ID 不变证据、26 个任务、每站页面清单、覆盖率、阻断修复与残余外部风险**
