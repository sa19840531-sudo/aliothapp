# Alioth V2 Explorer Agent：H5 现状与迁移补充设计

> 日期：2026-07-30
> 状态：待书面审阅
> 主规格：[2026-07-29-v2-explorer-agent-design.md](./2026-07-29-v2-explorer-agent-design.md)

## 1. 文档目的

本补充设计总结 Alioth 当前 H5 探索能力，明确哪些能力应直接复用、哪些结构需要在 V2 Explorer Agent 中解耦，并作为 Web Explorer/Judge 拆分的实施基线。

## 2. 当前 H5 探索能力

当前 H5 链路已经形成“采集 → 识别 → Frontier → 动作执行 → 补采 → 规则工作项 → 覆盖收口”的完整基础，不应在 V2 中推倒重写。

### 2.1 采集与会话

- 按任务视口创建和复用 Playwright 会话，移动 H5 可使用真实手机视口。
- `goto` 对连接关闭、连接重置、网络切换和空响应执行有界重试；证书和权限错误不重试。
- 采集截图、DOM、当前视口可见文本、UI Snapshot、HAR、Console、媒体状态、Action Trace、滚动覆盖和时序证据。
- `visible_text` 表达当前视口的视觉事实，排除被全屏广告、弹窗、图片或遮罩覆盖的底层文本。
- 截图二进制不进入模型文本载荷，重证据通过 Artifact ID 引用。

### 2.2 页面识别

- 使用独立多模态页面识别 Tool，读取完整证据后返回结构化页面类型、状态和候选动作。
- H5 Profile 可识别线路加载壳、启动广告、顶部遮挡、底部 Tab、页内 Tab、内容卡片和高风险入口。
- `page_settle_complete=false` 不再一律阻断；证据可用时允许带降级标记识别。
- 支持模型失败时基于可审计业务路由或强 DOM 结构做有限确定性回退。
- 支持认证门禁、弹窗状态、页面状态同义词归一和目录外类型拦截。
- 支持导航别名、DOM 属性语义入口以及 `ranking/settings/content_detail/video_detail/post_list` 等受控补强。

### 2.3 动态 Frontier

- 候选动作进入 WorkingMemory Frontier，按局部下钻、全局业务导航、来源恢复等语义调度。
- 同一逻辑入口可跨快照去重；来源快照被替代后，可按原因重绑或重新入队。
- 旧页内 Tab、旧局部 selector 和错误来源动作会被识别为 stale，不能在错误页面执行。
- 详情页优先下钻，返回后继续全局业务导航；底部 Tabbar 可跨快照保留。
- 动作实际结果必须经过页面采集和识别验真后，才能提交为“已探索”。

### 2.4 遮挡与动作安全

- 支持逐层关闭启动广告、普通 Dialog、Overlay 和业务弹窗；每次关闭后重新采集与识别。
- 优先使用可见且可证明的关闭控件，必要时使用经过证明的 Overlay 安全区域。
- 支持图片截获指针、底部导航无效果、轮播卡片离开视口等真实 H5 交互恢复。
- 页面级遮挡恢复有总预算，同 selector 无进展有更短预算；预算耗尽进入明确阻断。
- 浏览支付、VIP、钱包等只读页面可以放行；支付、购买、登录、提交、发布、上传、下载和安装等真实变更动作强制高风险。

### 2.5 页面质检、补采与完成门禁

- `page_capture_id` 是页面状态和证据归属的核心键。
- 候选规则、规则结果、补采替代和页面生命周期均有结构化状态。
- `ambiguous/insufficient` 只在瞬态或确需更多观察时生成有界补采；稳定歧义直接转人工复核。
- 滚动、时序和规则证据补采可以生成新 Capture，并通过 `supersedes_page_capture_id` 迁移上下文。
- 持久化 Inspection Work Item 已区分探索生产者和质检消费者，并具备租约、Attempt、恢复和完成门禁。
- 报告发布前校验 Frontier、工作项、规则成员和后台判定均已收口。

### 2.6 审计与测试基础

- `qc_agent_action_trace`、`qc_page_exploration_action`、`qc_task_page_node`、`qc_page_capture` 和 Evidence Artifact 已覆盖主要探索事实。
- 截至 2026-07-30，核心测试文件包含：
  - H5 Profile：8 个测试；
  - 页面采集：107 个测试；
  - 页面识别：141 个测试；
  - WorkingMemory：129 个测试；
  - 任务执行策略：24 个测试。
- 2026-07-22 的历史样本基线覆盖 48 个 H5 入口、40 个成功入口和 372 个结构快照，覆盖当时 14 类目标中的 13 类。该数据是设计样本，不等同于当前生产覆盖率；后续实现已继续补强 `video_detail` 等页面。

## 3. H5 当前架构问题

1. **探索与判定仍共享 Main Agent 边界**：虽然持久化工作项已经区分 producer/consumer，探索决策、规则派发和部分完成门禁仍在同一 Runtime/WorkingMemory 中耦合。
2. **状态模型以 Web 页面为中心**：`qc_task_page_node`、`qc_page_exploration_action`、`page_capture_id` 和 URL/selector 语义无法直接表达 APK Activity、原生 View 和静态图片。
3. **核心文件过大**：`page_detection_tools.py` 和 `page_capture.py` 已承载大量识别、动作、安全和恢复逻辑，继续叠加 APK 会违反单文件职责原则。
4. **恢复仍依赖活跃会话**：浏览器会话损坏后尚不能可靠跨进程恢复同一个 Explorer Run；当前主要通过有界失败、来源恢复和新 Capture 收口。
5. **H5 指标尚未成为 V2 基线**：测试覆盖强，但尚未统一形成 V1/V2 Shadow 的覆盖率、重复状态率、阻断率、证据完整率、成本和 P95 对账看板。
6. **任务输入仍是 URL**：上传截图和 APK 缺少统一的目标描述、存储引用、设备 Profile 和安全策略入口。

## 4. H5 向 V2 Explorer 的迁移原则

- **保留成熟行为，重构调用边界**：H5 Profile、Playwright 采集、页面识别、Frontier、遮挡清障和风险门禁原样复用，不重写业务语义。
- **先抽 WebTargetAdapter**：把浏览器执行和页面观察封装为 Adapter，Explorer 只看目标无关 Observation。
- **拆分超大文件职责**：页面识别确定性后处理、候选生成、遮挡策略和动作执行分别进入独立模块。
- **双写目标无关模型**：M1 期间同时写新的 Mission/State/Transition/Coverage 与旧 Web 投影，确保工作台和历史查询不受影响。
- **用 H5 作为 V2 第一基准集**：先用现有 H5 夹具和真实任务比较 V1/V2，再接图片和 APK。
- **不把站点经验写成 Agent 私有 Prompt**：可复用规则继续沉淀为确定性代码、Skill、目录和回归用例。

## 5. H5 拆分后的模块映射

| 当前能力 | V2 归属 | 处理方式 |
| --- | --- | --- |
| H5 Profile | WebTargetAdapter 的观察增强 | 原逻辑复用，输出 TargetObservation 信号 |
| 页面识别 Tool | Explorer 观察 Tool | 保留多模态和确定性回退，移除 Judge 写回职责 |
| 动态 Frontier | Explorer Runtime | 从 WorkingMemory 页面结构抽成目标无关状态图 |
| 页面动作执行 | WebTargetAdapter | 只暴露结构化低风险动作 |
| 动作风险模型 | Action Policy | 抽成目标无关策略，Web 和 Android 共用风险语义 |
| 页面证据采集 | Evidence Recorder | 保留 Artifact ID 交接 |
| 规则候选和工作项 | Orchestrator 交接 | Explorer 发布，Judge 独立消费 |
| 页面完成门禁 | Coverage + Orchestrator | Explorer 报告覆盖，Orchestrator 统一判定任务是否可收口 |

## 6. H5 第一阶段验收基线

M1 Web Explorer/Judge 解耦至少满足：

1. 同一 H5 任务创建独立 Explorer Run 和 Judge consumer Run。
2. Explorer Tool 列表不包含正式规则写回能力。
3. Judge Tool 列表不包含 Playwright 页面操作能力。
4. 原有 H5 Profile、遮挡关闭、底部导航、详情下钻、来源恢复和风险门禁回归全部通过。
5. V1/V2 对固定任务集输出相同的有效页面集合、证据归属和阻断原因；差异必须可解释。
6. 新的 Mission/State/Transition/Coverage 与旧 Web 表双写一致。
7. 发生预算耗尽、页面阻断或模型失败时，V2 不得把 `partial/blocked/failed` 显示为 `completed`。
