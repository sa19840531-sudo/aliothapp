# H5 页面探索完善度审计整改复审

## 范围

仅复审 R-01、R-02、H5 `exploration_only` Rule Engine 隔离和 H5 Cutover 路由口径；App 问题不在本次范围。复审基于 Tasks 1–5 的实现提交、RED/GREEN 记录、当前分支代码和 Task 6 受控验证，不把本地 Fake、Mock 或只读检查描述为真实联合验收。

`aef2e1f`、`26f5cff`、`669d9936` 是并发外部提交，已从 clean history 完全排除；它们既不在 `fc5e9823..55eba2b3` 原 clean range，也不在最新 `654e5a16...4c2b515d` 净差异范围内，不作为本轮整改、测试或交付证据，本次也未修改其文件。

## 逐项结论

### R-01：三类入口哨兵的首跳 Capture 父级

- **根因**：共享首跳信任 helper 只完整识别 H5 显式入口，`agent-entry:*` 与 `legacy-agent-entry` 被误判为普通父 Capture，导致 before Capture 持久化伪父级。
- **修改与提交**：`c766a5a`；修改 `services/api/app/application/evidence_exit_gate.py`、`services/api/tests/test_web_evidence_exit_gate.py`、`services/api/tests/test_capture_coordinator.py`。
- **RED 证据**：聚焦命令得到 `4 failed, 72 passed`，失败均指向 agent/legacy 哨兵被写入 before Capture 的 `parent_capture_id`。
- **GREEN 证据**：同一聚焦命令得到 `76 passed, 2 warnings`；参数化断言 `h5-entry:*` 需显式标记、另两类按既有合同可信，三类 before 父级均为 `None`，after 父级均为本次 before Capture。
- **最终状态与复审证据**：合入最新远程基线后的 exact HEAD `4c2b515d` 九文件定点回归为 `318 passed, 0 skipped, 0 failed`。本地合同闭合；真实 Playwright 首跳与历史任务回放未覆盖。

### R-02：H5 URL userinfo、非法端口与响应脱敏

- **根因**：H5 与 Web 共用基础 URL 校验，未拒绝 userinfo 和非法端口；Schema 拒绝后，FastAPI 默认校验详情仍会在 `input` 中回显原始凭据。
- **修改与提交**：`677d6c1`、`00d44f3`、最终纵深脱敏修复 `55eba2b3`；修改 `services/api/app/api/v2/schemas.py`、`services/api/app/core/errors.py`、`services/api/tests/test_v2_task_entrypoints.py`。
- **RED 证据**：Schema 边界用例先得到 `6 failed`，两个入口的 username、username+password、非法端口均错误进入 application entry；脱敏补强前再得到 `4 failed, 2 passed`，四个凭据用例的 422 响应仍泄露 `alice` 或 `secret`。
- **GREEN 证据**：聚焦安全与无关校验兼容用例 `7 passed`，完整入口文件 `29 passed`；`55eba2b3` 进一步将 H5 URL 错误 `msg` 固定为无敏感信息文案、将 `input` 替换为 `[已脱敏]` 并删除可能嵌套原始异常的 `ctx`，同时覆盖全角斜杠混淆输入。两个 application entry 始终零调用，无关校验详情合同保持不变。
- **最终状态与复审证据**：合入最新远程基线后的 exact HEAD `4c2b515d` 定点回归覆盖完整入口文件并通过。真实反向代理、重定向链、访问日志和请求体脱敏未联合验证。

### H5 `exploration_only` Rule Engine 隔离

- **根因**：规则配置在 Worker 启动期 eager 扫描，使不需要规则判定的仅探索任务也依赖规则仓储；同时缺少按任务模式、路由后执行的共享门禁。
- **修改与提交**：`453f95fa`、`68f2f09d`；修改 `services/api/app/application/rule_judge.py`、`services/api/app/worker.py`、`services/api/tests/test_rule_judge.py`、`services/api/tests/test_worker_h5_v2_route.py`、`services/api/tests/test_worker.py`。
- **RED 证据**：独立门禁首次收集因类型不存在得到 `1 error`；Worker 接线用例得到 `4 failed, 1 passed`，组合根精确注入用例另有 `1 failed`，均证明门禁尚未沿执行路径共享注入。
- **GREEN 证据**：Rule Judge 文件 `76 passed`；Worker 聚焦与最终回归分别 `5 passed`、`1 passed`、`111 passed`。`full_inspection` 在路由后验证并失败关闭，`exploration_only` 调用同一接口但规则仓储零访问，失败不缓存、并发成功只扫描一次。
- **最终状态与复审证据**：Task 6 定点与扩展回归均覆盖相关文件且无 failed/error。真实 PostgreSQL 短断、多 Worker 进程缓存、Redis 与 H5 浏览器 Runtime 未联合验证。

### H5 Cutover 的 shadow/on 快照路由

- **根因**：切换脚本把全局 `ALIOTH_WORKFLOW_VERSION` 当成所有目标档位的硬门槛，没有区分 shadow 旧入口快照与 on 档 V2 API 显式锁定快照。
- **修改与提交**：`f607d3e7`；修改 `scripts/check_h5_v2_cutover_prerequisites.py`、`services/api/tests/test_h5_v2_cutover_prerequisites.py`。
- **RED 证据**：新矩阵在旧实现上得到 3 项预期失败：shadow 检查名未表达旧入口合同，on+v1 被错误判定 `ready=false`，对应 CLI 错误返回 2。
- **GREEN 证据**：切换脚本、App 路由和 V2 入口共 `49 passed`；真实 CLI 矩阵确认 shadow+v1 以 `legacy_entry_default_locks_v2` 阻断并返回 2，on+v1 仅告警且返回 0，详情明确 V2 API 与任务快照。
- **最终状态与复审证据**：Task 6 定点与扩展回归均覆盖切换脚本测试且无 failed/error。真实 shadow 任务、on 灰度、Worker 重启和生产流量未验证。

## 验证证据

### Task 6 首轮受控验证

1. 九文件定点回归首次执行在 120 秒上限到达后被终止：退出码 `124`，无可判定测试摘要，状态记为**超时**，不得算通过。相同命令增加时限后重跑：退出码 `0`，`312 passed, 0 skipped, 0 failed, 3 warnings in 98.38s`。
2. `python -m pytest services/api/tests -k 'h5 or web_evidence_exit_gate or capture_coordinator or v2_task_entrypoints or rule_engine_guard' -q`：退出码 `0`，`554 passed, 5 skipped, 0 failed, 3416 deselected, 3 warnings in 35.34s`。
3. 为逐项确认跳过原因，以同一筛选条件加 `-rs` 重跑：退出码 `0`，仍为 `554 passed, 5 skipped, 0 failed, 3416 deselected`。跳过项如下：
   - `test_airtest_entry_rewrite.py`：Mobile Demo API 已下线，旧 Airtest helper 不再是正式门禁；
   - `test_airtest_shot_dedupe.py`：同上；
   - `test_app_architecture_boundaries.py`：Mobile Demo Agent Ad Capture 已下线，正式链路改由 App Workflow 承接；
   - `test_h5_evidence_inventory_postgres_integration.py`：未显式设置隔离的 `ALIOTH_TEST_DATABASE_URL`；
   - `test_h5_page_type_assessment_postgres_integration.py`：未显式设置隔离的 `ALIOTH_TEST_DATABASE_URL`。
4. 指定文件 Ruff：退出码 `0`，`All checks passed!`。
5. 指定文件 `compileall -q`：退出码 `0`，无错误输出。
6. `alembic -c services/api/migrations/alembic.ini heads`：退出码 `0`，唯一输出 `20260814_0057 (head)`。

### clean review 提交链与 exact runtime HEAD `55eba2b3` 新鲜复验

clean history 的首次 review 为 `170a9a97 [AI编程] doc: 完成H5审计整改复审`，提交后补证为 `0858f7de [AI编程] doc: 补全H5复审提交后证据`。以下结果均在最终运行时代码 HEAD `55eba2b3 [AI编程] fix: 纵深脱敏H5 URL校验错误` 上重新执行，不复用首轮输出：

1. 相同九文件定点 pytest：退出码 `0`，`314 passed, 0 skipped, 0 failed, 4 warnings in 81.22s`。
2. 相同 H5/共享扩展 pytest：退出码 `0`，`556 passed, 5 skipped, 0 failed, 3416 deselected, 4 warnings in 41.12s`；5 个 skip 的性质与首轮逐项记录一致。
3. 指定文件 Ruff：退出码 `0`，`All checks passed!`。
4. 指定文件 `compileall -q`：退出码 `0`，无错误输出。
5. `alembic -c services/api/migrations/alembic.ini heads`：退出码 `0`，唯一输出 `20260814_0057 (head)`。

本次后续变更只补充正式 review 文档，不改变 `55eba2b3` 已验证的运行时代码。

### 最新远程基线合入后的 exact HEAD `4c2b515d`

- 远程 `origin/dev_1.5` tip `654e5a16` 已通过 merge commit `4c2b515d [AI编程] merge: 同步最新dev_1.5至H5审计整改` 合入 clean 分支。
- 合并冲突仅发生在 `docs/专项能力与高频回归规则.md` 与 `services/api/app/application/evidence_exit_gate.py`；解析后没有未解决冲突。冲突聚焦回归为 `77 passed`，对应 Ruff 检查为 `All checks passed!`。
- 九文件定点 pytest：退出码 `0`，`318 passed, 0 skipped, 0 failed, 4 warnings in 100.89s`。
- H5/共享扩展 pytest：退出码 `0`，`568 passed, 5 skipped, 0 failed, 3536 deselected, 4 warnings in 92.77s`；5 个 skip 的性质维持既有逐项记录。
- 指定文件 Ruff：退出码 `0`，`All checks passed!`。指定文件 `compileall -q`：退出码 `0`，无错误输出。
- `alembic -c services/api/migrations/alembic.ini heads`：退出码 `0`，唯一输出 `20260814_0062 (head)`。迁移 `0058` 至 `0062` 来自最新远程基线，本 H5 审计修复自身没有数据库 migration。

本次后续提交仍只校准正式 review 文档，不改变 `4c2b515d` 已验证的运行时代码；主控将在文档提交后对 exact final HEAD 再次验证。

### 已知 warnings、skip 与最新 clean range 检查

- 最新 exact HEAD `4c2b515d` 的两个 pytest 批次均报告 4 条 warning：Microsoft Agent Framework 的 `SkillResource` ExperimentalWarning、`AgentFileStore` ExperimentalWarning、Starlette TestClient 的 `httpx` 弃用提示，以及 linked worktree pytest cache 路径权限导致的 PytestCacheWarning；本轮未消除，也未改写为通过项。
- 扩展回归的 5 个 skip 维持首轮逐项记录口径：3 个来自已下线 App/Airtest 旧链路，2 个因未显式设置隔离的 `ALIOTH_TEST_DATABASE_URL`；不得计入 passed。
- 在 exact HEAD `4c2b515d` 上，`git diff --check origin/dev_1.5...HEAD` 退出码 `0`；`origin/dev_1.5` 对应 `654e5a16`，`git diff --stat` 为 `16 files changed, 1491 insertions(+), 41 deletions(-)`。`git diff --name-only` 仅列出本轮计划内 H5/共享实现、测试、切换脚本及设计/计划/规则/复审文档。
- `services/api/app/application/evidence_exit_gate.py` 相对最新远程基线已没有生产代码净差异，但 `services/api/tests/test_web_evidence_exit_gate.py` 与 `services/api/tests/test_capture_coordinator.py` 的本轮共享入口合同测试仍保留并通过验证。
- 同一 HEAD 的 `git status --short --branch` 仅输出 clean 分支行，没有 tracked 变更。并发外部提交仍完全排除，净差异中没有 migration、App 功能、工作站代码或未知文件。

## 发布门禁

真实 PostgreSQL、Redis、Playwright、MinIO、LLM、认证、浏览器崩溃和 Worker 重启联合验收仍未执行；两个 PostgreSQL 集成用例因缺少显式隔离测试库而跳过。当前证据只证明本地代码合同与只读切换矩阵，不得据此批准生产放量。

## 变更影响

数据库迁移：本 H5 审计修复自身无 migration；最新远程基线合入 `0058` 至 `0062`，当前唯一 head 为 `20260814_0062`。前端菜单/路由：无需同步。App 产品合同：无变化。
