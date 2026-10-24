# 截图审查问题整改实施计划

## 1. 执行约束

- 实施分支：当前 `dev_1.5`。
- 保留工作区已有未跟踪文件，不纳入任何批次。
- 先写失败测试，再修改实现。
- 所有新增注释使用中文，文件保持 UTF-8。
- 每一批完成后依次执行：定向测试、静态检查、`git diff --check`、代码 review、变更范围确认，再提交 Git。
- 默认不新增数据库迁移、不修改前端菜单和路由；若批次 1 证明现有结构无法完成原子最终化，立即暂停并另行提交迁移设计。

## 2. 批次总览

| 批次 | 目标 | 主要模块 | 数据库迁移 | 前端同步 | 提交前置条件 |
|---|---|---|---|---|---|
| 0 | 设计与计划固化 | `docs/superpowers` | 否 | 否 | 已完成并提交 `2c9a469` |
| 1 | Completion Gate 原子最终化与恢复 | Worker、Task Finalization、PostgreSQL 状态存储 | 否 | 否 | PG 触发器回归、幂等和恢复测试通过 |
| 2 | Android Runtime 事实、完成门禁和危险点击 | Android Device、Mobile Demo Agent | 否 | 否 | 模型伪造证据测试失败，真实节点测试通过 |
| 3 | Artifact、路径、Sandbox 和代理配置 | Capture Persistence、Mobile Demo Host、部署配置 | 否 | 否 | 安全、配置和对象清理回归通过 |
| 4 | 规则文档和全量验收 | `docs/专项能力与高频回归规则.md`、全仓测试 | 否 | 否 | 全量验证、最终审计和未覆盖风险清单完成 |

## 3. 批次 1：Completion Gate 原子最终化

### 3.1 失败测试

1. 在 `services/api/tests/test_worker.py` 增加回归测试：Gate 通过后，Worker 不得再通过 `record_run_finished()` 改写已封存任务的 Agent Run。
2. 在 Task Finalization repository 测试中增加事务顺序断言：最终 Agent Run 写入、Gate 绑定必须发生在任务更新为 `sealed` 之前。
3. 增加报告生成失败、重复调用和 Worker 重启后的幂等测试。
4. 增加 `exploration_draining` 已提交后可被 `list_recoverable_finalizations()` 返回的测试。
5. 对已 `sealed` 但已有最终报告、尚未绑定的历史记录，验证恢复只补绑定，不更新 Agent Run。

### 3.2 实现任务

1. 在 `services/api/app/application/task_finalization.py` 增加最终报告最终化命令和服务契约，携带任务、Agent Run、Gate 预期版本、报告 ID/版本/哈希及最终报告内容。
2. 在 `services/api/app/infrastructure/db/task_finalization_repository.py` 增加单事务最终化实现：锁定任务和 Agent Run，重新计算 Gate，写入最终 Agent Run，创建快照，插入报告绑定，最后封存任务。
3. 保留现有封存触发器，不增加绕过字段或特殊权限。
4. 修改 `services/api/app/worker.py` 的终态路径：
   - 非 Gate 流程维持现有语义；
   - Gate 通过时调用原子最终化服务；
   - 不在 `sealed` 后调用会改写 Agent Run 的状态存储方法；
   - 最终报告失败时保留可恢复状态并记录审计原因。
5. 修改恢复路径，使 `exploration_draining` 先安全推进到 Session Draining，再走幂等 Gate/报告处理；已封存未绑定记录只允许补绑定。
6. 检查 `record_finished`、`record_run_finished` 的调用边界，确保所有封存后的调用都不再产生 Agent Run 更新。

### 3.3 批次 review 清单

- SQL 中不存在封存后写 Agent Run 的路径。
- 绑定记录不能先于 Gate 快照，任务封存必须是事务最后一步。
- Gate 失败、报告失败、重复执行和重启恢复都不会重复创建绑定。
- 触发器仍拒绝外部封存态 Agent Run 改写。
- 旧接口的 Fake Service 和架构边界测试均同步更新，没有保留第二套最终化路径。

### 3.4 验证与提交

定向运行 Worker、Task Finalization、状态存储和迁移契约测试；若本地 PostgreSQL 测试服务可用，再运行真实触发器集成测试。通过后提交：

`[AI编程] fix: 收口 Completion Gate 原子最终化；模块：Worker/Task Finalization/PostgreSQL；数据库迁移：否；前端菜单路由：否；测试风险：真实PG恢复矩阵需验证`

## 4. 批次 2：Android Runtime Observation 与策略门禁

### 4.1 失败测试

1. 新增 Runtime Observation 解析测试，覆盖 UI hierarchy 节点、边界、可见性、角色和安全 Tab 候选。
2. 修改 `services/api/tests/test_mobile_demo_agent_ad_capture.py`：仅伪造模型 `exploration_evidence` 时，完成校验必须失败。
3. 增加真实 Runtime Observation 表示安全 Tab 已耗尽时的完成通过测试。
4. 增加缺少 UI hierarchy、解析失败和节点过期时的阻断测试。
5. 增加危险点击测试：模型伪造 `target_id`/`bounds`、坐标落在非目标节点、节点不可见或语义不允许时均阻断；真实匹配节点时才允许。
6. 修改 Android action intent 测试，确认 Tap 不再固定为 `InteractionRiskLevel.LOW`。

### 4.2 实现任务

1. 新增单一职责模块 `services/api/app/infrastructure/device/android_runtime_observation.py`，定义不可变节点、边界、观察结果和安全 Tab 事实。
2. 扩展 `services/api/app/infrastructure/device/adb_ops.py`，提供受控 UI hierarchy 输出；不把任意 shell 执行能力暴露给上层。
3. 在 `android_surface_driver.py` 中解析 ADB 结果，明确 capability 为 READY、PARTIAL 或 UNAVAILABLE，并携带采集时间和设备上下文。
4. 在 `android_capture_runtime.py` 中提供最近一次 Runtime Observation，动作前刷新观察，动作后重新采集，避免使用过期节点。
5. 在 `mobile_demo_agent_ad_capture.py` 中把完成事实更新改为消费 Runtime Observation；模型字段只保留为解释和审计摘要。
6. 重写危险关闭控件校验：使用真实节点的角色、可见状态、语义标签、边界和点击点匹配；无可靠观察时默认阻断。
7. 按动作语义计算风险等级，至少将任意 Tap 与危险关闭/下载类动作从低风险路径移出。
8. 更新能力清单和相关契约测试，但不把 UI tree、visible text 或 OCR 添加到现有 App Capture Artifact 类型。

### 4.3 批次 review 清单

- 完成状态没有任何从模型 JSON 直接赋值的路径。
- Runtime Observation 不会被模型输入覆盖，也不会把未验证文本当作策略指令。
- 节点边界使用同一坐标系，设备尺寸变化不会绕过匹配。
- 观察不可用、节点失效和动作语义不确定时全部 fail closed。
- App Capture 仍满足 screenshot/action_trace 约束，未意外扩展 Artifact 契约。

### 4.4 验证与提交

运行 Android driver、Runtime Observation、Agent 完成和策略测试，并运行架构边界测试。真实设备不可用时必须保留明确的设备集成测试结果和模拟器覆盖说明，不以模拟测试替代真实设备验收。通过后提交：

`[AI编程] fix: 以Android Runtime事实收口代理门禁；模块：Android Device/Mobile Demo Agent；数据库迁移：否；前端菜单路由：否；测试风险：真实设备UI层级差异需现场验证`

## 5. 批次 3：对象、路径和部署边界

### 5.1 失败测试

1. 在 `test_app_capture_persistence.py` 增加历史 UUID4 Artifact 被复用时，新建未引用对象必须被删除的测试。
2. 增加 Artifact 保存成功、数据库保存失败和重复引用三类补偿测试，确保不删除数据库已经引用的对象。
3. 在 Host 安全测试中覆盖 `..`、`.`、斜杠、反斜杠、空值和超长 `run_id`，并覆盖内部路径辅助函数及所有 HTTP 路由。
4. 增加 Sandbox 启动脚本测试，确认选择的实际环境文件被强制传入 Compose 服务 `env_file`，不会使用示例自引用路径。
5. 增加 Production Compose 配置测试，确认精确代理地址被显式注入 API，缺失配置时保持 fail closed。

### 5.2 实现任务

1. 修改 `services/api/app/application/app_capture_persistence.py`，记录本次调用创建的对象；数据库返回历史 Artifact ID 后，对未被引用且由本次调用创建的对象执行可审计补偿清理。
2. 在 Mobile Demo Host 共享路径模块中增加安全 `run_id` 校验，并在 `_run_dir()`、JSON/日志路径和所有路由入口执行。
3. 修改 `infra/docker-compose.emulator-sandbox.yml` 使用独立的真实环境文件选择变量；修改 `scripts/start-emulator-sandbox.sh` 在调用 Compose 前强制设置该变量；同步修正文档示例。
4. 检查其它 Sandbox Compose 入口，避免保留同类自引用路径。
5. 修改 `infra/docker-compose.production.yml` 与 `infra/env/compose.production.example`，显式注入 `ALIOTH_MOBILE_DEMO_TRUSTED_PROXY_ADDRESSES`；保持精确地址白名单，不接受宽泛网段作为代理信任。

### 5.3 批次 review 清单

- 所有路径构造均在受控 `runs` 根目录内。
- `run_id` 校验不会破坏当前合法生成值和已有稳定接口。
- Artifact 清理不会删除历史对象或数据库已引用对象。
- Compose 的插值值、服务 `env_file` 和容器实际环境三者一致。
- 代理信任配置缺失或错误时只会拒绝请求，不会扩大可信边界。

### 5.4 验证与提交

运行 Artifact persistence、Host security、Compose contract 和 Mobile Demo 配置测试，执行 Compose 静态 config 检查（不启动真实服务）。通过后提交：

`[AI编程] fix: 收紧Mobile Demo对象路径与部署边界；模块：Capture Persistence/Host/Compose；数据库迁移：否；前端菜单路由：否；测试风险：生产容器网络地址需部署环境复核`

## 6. 批次 4：规则文档与最终验收

### 6.1 文档任务

更新 `docs/专项能力与高频回归规则.md`，新增可复用规则：

- sealed 后禁止 Agent Run 写入；报告绑定必须与最终化事务一致。
- Agent 完成和危险动作证据必须来自 Runtime Observation。
- 恢复扫描状态集合必须覆盖每个已提交的中间终态。
- 文件路径、Artifact 补偿清理和 Compose 环境选择必须有边界测试。
- 生产代理信任只能使用显式精确地址。

### 6.2 最终验证

1. 运行全部后端单元测试和定向回归测试。
2. 运行 Ruff、格式和导入检查。
3. 对迁移 SQL、Compose 文件、Shell 脚本和 Python 路径代码执行静态审查。
4. 检查 `git diff --check`、中文注释、UTF-8 和单文件职责。
5. 对截图 5 个 P1、4 个 P2 和 P3 历史风险逐项建立“已修复/已验证/仍需外部环境”清单。
6. 确认未修改前端菜单、路由和用户已有未跟踪文件。

### 6.3 提交

`[AI编程] doc: 补齐截图整改回归规则与验收记录；模块：工程规则/全量验证；数据库迁移：否；前端菜单路由：否；测试风险：真实Android与生产网络需部署验收`

## 7. 停止条件与风险处理

- 任一批次出现新的 P1、数据不可逆风险或需要数据库迁移，立即停止该批次提交，先更新设计和迁移方案。
- 真实 PostgreSQL、模拟器或生产 Compose 环境不可用时，记录缺失证据，不宣称全量验收通过。
- 不删除、重置或覆盖用户已有未跟踪文件；不重写既有 Git 历史。
