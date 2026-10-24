# H5 Adaptive Runner 接线与检查点精确证据绑定

## 范围与结论

2026-10-16，需求分支 `h5-chain-reliability-and-entry-compatibility`，基准提交 `c9adb731ab1f5209b8ae1d43b8daed9443d71afd`。工作区已有大量未提交修改，本次未切分支、提交、推送、重载、运行原站任务或部署。

已接入生产组装入口与主探索执行路径，补充检查点尝试、动作账本、前后 Capture、附件、最新 Manifest 版本的精确绑定和只读复核。**这是执行与绑定层，不是流程最终质检已经完成。** 通用 Judge、流程 InputSet 和正式报告消费尚未接齐，V2 流程仍阻断 Completion Gate。不能把 `observed`、`bound`、Manifest 质量合格或回归数量当作规则通过。

成功标准：已有无计划 H5 探索仍复用原执行器；检查点只在现有安全候选中重规划；预算和截止不可变化；动作经原 Coordinator；重投递不重放；证据串属、旧帧和最新失败版本不能获得就绪；最终 Gate 不因绑定而放行。

## 实际接线

- `PostgresH5RuntimeProvider.build` 装配 `H5AdaptiveJourneyRunner`、检查点仓储、可信快照观察器和惰性 H5 MAF 规划器。
- `H5ExplorationTurnRunner` 在现有整段会话旅程与工作续租围栏内部执行准备、持久化工作载荷、执行和后续探索。没有另建浏览器、另发点击或绕过现有来源/风险合同。
- 只从同来源 Capture、同状态版本的 selected/pending 低风险候选中选择；既有调度器、操作边界、深度与封禁集合继续生效。无关的唯一候选不自动属于检查点。仅已签发原生视频全屏合同与相应目标匹配时允许确定性选路。
- 无 Run 计划时不创建模型规划器、不预留尝试，普通执行器只调用一次。普通路径新增计划读取开销；不声称零数据库开销。
- 模型沿用现有 H5 Explorer 和物理调用预算上下文，未建立第二份预算、增加调用上限或刷新冻结期限。隔离测试使用模型桩，不提供真实 Token/CNY 或模型有效产出结论。

## 精确绑定

新迁移 `20261016_0136` 继承 `0135`，仅在自有隔离 PostgreSQL 应用：

| 记录 | 内容与边界 |
| --- | --- |
| `qc_journey_checkpoint_attempt` | 冻结 Plan/Checkpoint/Task/Run/Session/Epoch/Work/Source/Intent，保留原工作和动作合同；每 Work 一个尝试 |
| `qc_journey_checkpoint_evidence` | 原始动作前后 Capture、动作 ID、Manifest ID/版本/哈希、附件 ID、检查点合同哈希与合同声明的业务角色 |
| `qc_journey_checkpoint_observation` | 晚到最新 Manifest 的追加式观察记录；不覆盖原绑定，不再次执行动作 |

绑定必须读取真实数据库行，并核对：

1. 动作实际完成，原 Intent、来源、会话、Epoch、命令序号完全一致；动作创建不早于检查点尝试。
2. 两个不同 Capture 的实际角色为 `before_action`/`after_action`；父链为 Source → Before → After，动作账本前后 ID 一致。
3. Capture 不早于尝试；前帧不晚于动作开始，后帧不早于动作结束。旧 `probe_before`、改标签的旧帧、跨 Run/Session/Epoch 均不能补造本次证据。
4. Coordinator 返回引用的角色、父链、动作和完整附件集合也须与实际行一致，不能只校验数据库忽略返回引用。
5. 使用 Capture 的最新 Manifest，并校验载荷哈希、实际版本、附件类型/路径/校验和与完整集合。待定/失败不能回退旧 pass。
6. 相同 PNG 原样保留但不计独立视觉帧；快照时钟或元数据变化不能把相同 PNG 补算独立帧。绑定业务角色不代表对应证据义务已经满足。

原结果读取再次核对完整身份、当前 Epoch 和原 Work 合同。恢复尝试在决策之前，已绑定直接读取原结果，reserved/gap 保持明确阻断；不调用模型生成第二个合同，也不重放动作。并发准入只有一个 `new`，其余同合同请求返回 `uncertain`，不同合同拒绝。

## 观察与 Gate

当前只注册有可信 UI 事实的原生字段，如单一媒体实例、可见性、已加载尺寸/readyState、暂停/播放状态和原生视频全屏。跨媒体实例、文档时间源变化、降级/截断 DOM、图片全屏、缺质量合格清单保持未就绪。

未接线的稳定后加载帧、暂停后连续时间停止、多帧义务、动作前正式页面分类和网络义务在规划前阻断，不为无法测量的合同先消耗模型和动作。原前后帧只能证明前后状态，不能证明暂停后时间持续停止。示例播放器合同含这些尚未完成义务，不能宣称整条播放器流程已运行成功。

必需 V2 流程在通用 Judge 正式接线前作为 `journey_results_incomplete` 阻断共享 Gate 事实读取，预检与正式报告封存使用同一入口。该缺口不是仍有执行资源在途，不引入无限等待 sealed。观察就绪、资源排空和业务通过继续分离。

## 复现与验证

验证目录：`docs/validation/2026-10-16-adaptive-journey/`。保留第一次失败和后续结果，**不把修测试夹具的错误冒充生产根因**。

| 验证材料 | 说明 |
| --- | --- |
| `red.xml`、`pause-red.xml`、`goal-red.xml`、`gate-red.xml` | 未接线、单帧时间误推断、无关唯一候选、缺流程 Gate 防线 |
| `ownership-isolated-red.xml`、`late-and-retro-red.xml` | 返回引用伪造、旧 Epoch、追认已发生动作的反例 |
| `reuse-red.xml`、`concurrent-red.xml` | 重领仍重新决策、原结果读取身份缺口、并发冲突错误分类 |
| `evidence-role-red.xml`、`before-origin-red.xml`、`duplicate-frame-red.xml` | 业务角色绑定缺失、早于尝试的旧帧、同 PNG 被元数据变化补算独立帧 |
| `measurement-admission-red.xml`、`fullscreen-identity-red.xml` | 未实现测量的准入，以及图片全屏/截断 DOM 误观察 |
| `postgres-second.xml` 等早期结果、`actual-gate.xml` | 包含 SQL 参数类型、测试校验和格式、外键清理顺序、测试枚举等开发/夹具错误，不能据此断言业务环境故障 |
| `final-regression-v3.xml` | 最终相关单元/应用回归结果；覆盖旧 Journey、主探索、Coordinator、会话互斥、InputSet、观察包与 Gate |
| `final-postgres-v4.xml` | 最终显式隔离 PostgreSQL 验证，覆盖新迁移/绑定/并发、Run 计划兼容、会话互斥、不确定在途和旧会话补证 |
| `source-sha256.txt` | 本轮验证源码指纹；旧轮次指纹不回填 |

最终结果：相关回归 **656 passed**，隔离 PostgreSQL **67 passed**，合计 **723** 个不重叠用例通过。Ruff 与 `git diff --check` 通过；两项 MAF 实验性接口警告保留，不是业务验收结果。中途名为 `reuse-green.xml` 的结果仍含失败，已保留，不纳入最终通过数；最终以以上两个明确 XML 为准。

数据库行与 UI 快照是受控测试材料，不是原站新一轮 DOM、截图或业务验收。没有调用真实模型、支付后台或原站播放器。原生全屏事实正反例只证明观察器与精确绑定规则，不证明稳定播放、真实视频详情正式覆盖、支付覆盖或没有回退。

隔离库：`127.0.0.1:56436/alioth_adaptive_journey_final_test`。初次失败使用的另外两个自有测试库保留；业务库、当前 API/Worker/前端和历史证据没有改动。测试结束仅退出自有隔离 PostgreSQL，保留目录与测试数据库文件。

## 剩余阻断

1. 流程 InputSet、通用 Judge 的正式判定、逐义务缺口与正式报告展示尚未完成，不能解除 V2 流程 Gate 缺口。
2. 稳定后加载帧、连续时间观测、多帧去重、动作前正式页面分类、网络测量和新实例恢复策略尚未接入本检查点路径。合同里的恢复策略不是启动新会话的执行许可。
3. 检查点当前是单次动作尝试绑定；未完成通用多动作场景的跨动作业务角色集合。不用额外重试或循环补证伪装完成。
4. 没有现网迁移/重载和原站代表性验收；支付与真实视频正式覆盖的无回退结论仍未验证。后续须先完成上述消费链路，再按授权开展真实任务验收。
5. 后台依赖型充值缺只读契约，私有高清配对缺原站声明材料，继续阻断，不按接口名或 URL 猜白名单/对应关系。

本次不写原表，不合并、不发布，不提高预算、不放宽风险或 Gate。隔离回归通过仅用于工程边界验证。
