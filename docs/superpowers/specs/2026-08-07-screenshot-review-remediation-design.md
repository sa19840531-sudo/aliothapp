# 截图审查问题整改设计

## 1. 背景与目标

本设计针对 `dev_1.5` 当前 HEAD `53151e6` 的截图审查结果，覆盖 Completion Gate、Android Explorer 运行时事实、危险点击防护、终态恢复、Sandbox 配置和配套安全问题。

目标是让以下不变量在真实 PostgreSQL、Worker 重启和 Android Runtime 场景下成立：

1. 任务进入 `sealed` 后不再发生任何 `qc_agent_run` 写入。
2. 最终报告写入、Gate 绑定和任务封存具有可恢复的一致性。
3. Agent 完成证据必须来自真实 Runtime Observation，模型 JSON 只能作为解释信息。
4. 危险点击必须匹配真实 UI 节点，不能由模型伪造节点身份或边界。
5. `exploration_draining` 崩溃后能够被扫描并继续处理。
6. Host 文件路径、Artifact 对象、Sandbox 环境文件和代理信任边界均保持在显式安全范围内。

## 2. 已确认的问题与根因

### 2.1 Completion Gate 与 PostgreSQL 终态触发器冲突

`task_finalization_repository.evaluate_and_seal()` 会将任务置为 `sealed`，Worker 随后仍调用 `record_run_finished()` 更新 `qc_agent_run`。迁移 `20260803_0032` 的 `qc_guard_agent_run_finalization` 对 `finalizing` 和 `sealed` 任务拒绝任何 Agent Run 更新，因此真实 PostgreSQL 路径会在 Gate 已通过后失败。

### 2.2 完成证据由模型自报

`mobile_demo_agent_ad_capture.py` 从模型 JSON 中读取 `exploration_evidence` 并设置 `safe_tabs_exhaustion_observed`。当前 Android 驱动只有截图能力，没有可供完成校验使用的 UI hierarchy、可见文本或 OCR 运行时事实。

### 2.3 危险点击只校验模型提供的绑定信息

`_is_bound_dismiss_control()` 只检查模型给出的 `source`、`target_id`、`role` 和 `bounds`，没有查询当前 UI 节点。Tap Intent 还被固定标记为低风险，不能表达危险控制操作。

### 2.4 恢复扫描遗漏 `exploration_draining`

`list_recoverable_finalizations()` 目前只扫描 `session_draining` 和未绑定报告的 `sealed`，进程在 `exploration_draining` 提交后崩溃会丢失恢复入口。

### 2.5 配置和边界问题

- Sandbox Compose 的服务 `env_file` 可能继续解析复制后的示例路径。
- Artifact 持久化复用历史 UUID4 时，新建但未被数据库引用的对象没有补偿清理。
- Host `run_id` 未限制路径字符，可能越出 `data/runs`。
- Production Compose 没有显式注入 Mobile Demo 的精确代理地址。

## 3. 方案比较

### 方案 A：放宽封存后的 Agent Run 触发器

改动较少，但会破坏 `sealed` 的不可变边界，并允许终态后继续修改审计数据。不采用。

### 方案 B：Gate 只读预检加原子最终化，推荐

先在非封存态读取 Gate 事实并生成最终报告，再通过单个数据库事务完成 Agent Run 最终写入、Gate 快照、报告绑定和任务封存。封存后只允许读取或补齐不涉及 Agent Run 改写的历史报告绑定。

该方案保留现有触发器的安全约束，不需要绕过令牌，适合真实 PostgreSQL 和重启恢复。

### 方案 C：新增终态写入令牌

增加数据库字段或事务令牌，让触发器只允许特定最终化写入。该方案需要迁移并扩大权限语义，复杂度和审计风险高于方案 B，不采用。

## 4. 推荐架构设计

### 4.1 Completion Gate 原子最终化

新增面向最终报告的最终化服务入口，事务内按以下顺序执行：

1. 锁定任务和目标 Agent Run，并校验任务仍处于可最终化状态。
2. 重新计算 Gate 事实，不能直接信任预检结果。
3. Gate 不通过时只返回阻断决策，不改变封存状态。
4. Gate 通过时，在任务尚未 `sealed` 时写入最终 Agent Run 内容。
5. 创建 Gate 快照和报告绑定记录。
6. 最后将任务更新为 `sealed`。

Worker 不再在 `sealed` 之后调用会改写 `qc_agent_run` 的方法。恢复服务对 `exploration_draining` 先补齐 Session Draining，再按相同的幂等路径处理。`finalizing` 作为事务内短暂状态，不作为正常恢复入口；历史残留记录必须审计并安全阻断。

### 4.2 Android Runtime Observation

在设备基础设施层增加结构化 Runtime Observation，至少包含：

- 节点标识、角色、资源 ID、文本或内容描述摘要；
- 可见、可用、可点击状态；
- 屏幕坐标边界；
- 当前包名和 Activity；
- 安全 Tab 候选、已观察 Tab 和未探索 Tab。

由 ADB UI hierarchy 采集并解析，采集失败必须显式记录为不可用。该对象只服务于策略判断、完成校验和审计摘要，不改变 App Capture 的截图优先 Artifact 契约。

### 4.3 完成门禁与危险动作

- 完成门禁只接受 Runtime Observation 计算出的 Tab 耗尽事实。
- 模型 `exploration_evidence` 不得直接改变完成状态。
- 危险动作必须通过真实节点匹配：节点角色、可见状态、边界和语义均由 Runtime Observation 提供。
- 找不到真实节点、节点边界不匹配或 Runtime Observation 不可用时，动作阻断。
- Tap 风险等级由动作语义计算，不能固定为 `LOW`。

### 4.4 其他安全修复

- 引入统一 `run_id` 安全校验，在内部路径构造和所有 Host 路由入口双重执行。
- Artifact 写入采用“记录创建对象，数据库落库后清理未引用对象”的补偿模式。
- Sandbox 使用独立的真实环境文件选择变量，由启动脚本覆盖 Compose 插值值，避免复制示例后的自引用。
- Production Compose 将精确代理地址作为显式部署配置注入 API，缺失时保持 fail closed。

## 5. 兼容性与非目标

- 不新增前端菜单、路由或页面。
- 不放宽终态 PostgreSQL 触发器。
- 不把完整 DOM、可见文本或 OCR 结果写入现有截图 Artifact。
- 不重写既有 Git 历史；历史合并冗余只记录为工程风险。
- 默认不需要数据库迁移；若实现阶段发现现有字段无法支撑原子绑定，将先暂停该批次并提交单独迁移设计，不隐式扩大数据结构。

## 6. 验收标准

1. 真实 PostgreSQL 测试证明 Gate 通过后不再出现封存态 Agent Run 写入冲突。
2. Gate 阻断、报告生成失败、Worker 重启和重复恢复均保持幂等且可审计。
3. 模型伪造 `exploration_evidence` 或 `dismiss_evidence` 的测试必须失败。
4. 基于真实 Runtime Observation 的合法完成和合法关闭动作测试通过。
5. `exploration_draining` 恢复测试覆盖提交后重启。
6. Artifact、`run_id`、Sandbox 和代理配置均有针对性回归测试。
7. 后端全量测试、静态检查和变更规则文档审查通过。
