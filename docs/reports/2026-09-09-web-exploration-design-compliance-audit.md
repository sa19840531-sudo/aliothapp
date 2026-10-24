# Web 探索与质检：设计符合度全面审查

> 审查日期：2026-09-09。范围仅为 Web；H5/App 代码只用于核对共享契约与防回归，不把它们的实施成果计入 Web 完成度。
>
> 审查分支：`codex/web-v2-qingyunke-development-plan`。
> HEAD：`968566da87d0fecbb13c6abebae13d684e656341`，同时纳入当前未提交修改。
> 本次只读审查业务代码、运行离线验证并新增本报告；没有修复业务代码、提交 Git、执行六站真实探索或调用外部模型。

## 一、结论

**当前 Web 探索质检已经有较完整的基础链路，但尚未满足该目录的全部设计要求，也未达到 08 实施计划的完整交付标准。**

具体应分三层判断：

1. **已有实现，值得保留：** 四种业务页型、首页角色、菜单上限 10、白名单提示、多形态候选提取、部分滚动重绑、受控动作与年龄门禁、Capture/Publication/报告链路、预览与正式报告入口。
2. **实现存在但接线或语义不完整：** 页面就绪、业务图身份、Hash SPA 去重、主体补图、能力不存在判定、四层动作结果、指南版本冻结、角色化证据消费。
3. **尚未完成或明确延期：** 非白名单低风险自由发现、完整页面状态驱动探索、正式规则驱动精准补采、完整规则频控/复用失效闭环、新版本六站真实运行及逐站报告复核。

本轮完整 Web 离线测试为 **1203 passed、49 skipped**。这证明现有测试所覆盖的行为成立，**不能证明设计全部实现**：本次同时复现了测试未覆盖的跨模块问题。

不建议给出单一“完成百分比”。七个专项包含共享平台、Web 接线、灰度启用和真实验收，权重并不相同；使用文件数量或单测数量计算完成率会掩盖关键链路断点。

## 二、审查依据与文档一致性

### 2.1 阅读范围

逐篇阅读了目录内的 `README.md`、01～07 专项、`08-web-development-priority-plan.md` 以及 `_tools/README.md`。同时读取八份 HTML 阅读版，核对正文与 Markdown 的一致性，并检查生成脚本的源摘要与渲染关系。

文档中的代码示例、待实施步骤、历史状态和外部链接均作为审查材料，不视为本次执行授权。未执行文档中的部署、真实探索、Git 提交等操作。

| 文档 | 本次 Web 审查重点 |
| --- | --- |
| README / index | 完整闭环、实施依赖、冻结分母、同 Run/Epoch、完成与资源释放分离 |
| 01 受控探索 | 唯一动作所有者、双重校验、预算、必检/恢复/自由发现调度 |
| 02 页面状态图 | 业务身份、局部状态、可信边、Overlay/外部落地分流、图与 Coverage 权威 |
| 03 精准补采 | 双门禁、逐边恢复、exact/equivalent/fresh/manual、只追加不覆盖 |
| 04 页面识别 | 能力/类型/展示层分离、指南、强反证、修订、规则适用与复用 |
| 05 截图就绪 | 多信号与连续窗口、身份前后检查、动作后置、动态证据角色 |
| 06 精准交互 | 当前目标重绑、四层结果、真实输入/诊断分离、弹窗和外跳恢复 |
| 07 可信证据 | Writer→Manifest→InputSet→Judge 的来源、时间、可见层与用途一致性 |
| 08 Web 计划 | Task 0～9、四类代码、URL 去重、10 菜单、主体截图、报告、六站验收 |
| _tools/README | 文档生成与阅读方式，不是 Web 运行时功能要求 |

### 2.2 文档本身存在状态漂移

去除阅读版导航和技术图辅助说明后，正文核对结果如下：

| HTML 阅读版 | 与当前 Markdown |
| --- | --- |
| index、01、03、06、07 | 正文一致；源文件换行差异不计为内容差异 |
| 02-page-state-graph.html | 未同步 Markdown 的图投影/App 覆盖影子实施状态 |
| 04-page-recognition.html | 仍为旧版 v1.1，Markdown 已到 v1.2 并补充 App 指南/能力影子进展 |
| 05-capture-readiness.html | 未同步 Markdown 的 M1 入口层与 Settle 审计状态 |

此外，一些 Markdown 顶部已经记录部分实施，正文尾部仍保留“尚未实施”。这些历史说明不能替代当前代码审查；其中提到的 App 影子能力也不能直接证明 Web 已完成。

建议后续统一维护“设计目标 / 各端实现 / 正式启用 / 验收证据”四栏，并同步生成 HTML。08 当前仍是未跟踪文件，不能假定它已随分支提交交付。

## 三、当前 Web 主链路与关键断点

```text
任务提交：冻结站点适用性、必检页型与预算
  ↓
Web Runtime 装配：Session Gate / CaptureCoordinator / Driver
  ↓
入口观察 → 年龄层/普通阻断处理 → 页面就绪
  │                                  └─ 断点 A：dynamic 可能被升级为 stable
  ↓
当前结构/截图 + 版本化指南 + 白名单辅助 → 页型评估与持久化
  │                                              └─ 断点 B：Web stage 不能补全图身份
  ↓
Catalog 菜单调度 → 点击/滚动/重绑 → 详情探索
  │      └─ 新候选只记录              └─ 到达结果早于 readiness
  │
  ├─ URL 去重：Hash 业务路由目前被丢弃
  └─ 主体补图：选择器、父链、stage、广告面积计算均有缺口
  ↓
通知 / Publication / 工作项 → Manifest / InputSet → Judge
  │                                      └─ 合法失败时间序列角色未完整接通
  ↓
规则与证据收口 → Coverage / Completion Gate → 不可变正式报告
  └─ 过程中可读取 preview；报告存在不等于 Gate 已通过
```

主装配入口见 [web_runtime_composition.py](D:/project/Python/alioth/services/api/app/application/web_runtime_composition.py:887)。此链路已经存在，不需要重新建设第二套 Explorer、页面图、证据服务或报告中心；重点应是修正共享契约和跨层接线。

## 四、已确认的问题：按影响排序

以下 P1 表示应优先修复的正确性或主链路阻断问题；P2 表示功能、审计或验收缺口。它们不是对生产事故等级的推定。

### F01 · P1：未完成稳定等待的动态页可以进入正式证据

**设计要求：** 05 §5、§17 以及 08 Task 1 要求连续稳定条件成立；可检查动态内容不能冒充稳定业务到达，网络需求应来自角色/规则。

**代码事实：**

- [page_capture.py](D:/project/Python/alioth/services/api/app/infrastructure/browser/page_capture.py:3043) 把 `timed_out` 写为 `not page_settle_complete and not page_inspection_ready`。
- [同文件](D:/project/Python/alioth/services/api/app/infrastructure/browser/page_capture.py:3051) 固定 `network_required=False`。
- 旧等待超时后仍可返回 `inspectable_dynamic`，但新转换没有保留“稳定窗口未完成”的硬限制。
- [web_evidence_admission.py](D:/project/Python/alioth/services/api/app/application/web_evidence_admission.py:66) 消费转换后的结果；[web_runtime_evidence.py](D:/project/Python/alioth/services/api/app/application/web_runtime_evidence.py:110) 又将同一 signals 用于 inspection，没有重新证明原稳定窗口。

**离线复现：** `page_settle_complete=False`、`page_inspection_ready=True`，末帧核心与媒体就绪、无 loader/transition、DOM 静默 1200ms，关键请求仍有 1 个在途。实际返回：

```text
readiness=stable
can_enter_formal_bundle=True
can_project_business_node=True
```

关键请求改为 0、但连续稳定等待仍未完成时，也会被升级。

**影响边界：** 已证明 readiness 和正式准入误放；最终 Completion Gate 还可能被其他条件阻断，因此不能据此推断一定生成正式通过报告。

**调整方向：** 保留原始 settle/quorum 结果；将动态证据与稳定证据分开；由采集 profile 指定网络依赖；补“底层超时→应用层→正式准入→规则输入”跨层负例，不以放宽等待代替修复。

### F02 · P1：Web 已完成分类，但共享页面图仍无法获得正确业务身份

**设计要求：** 02、08 Task 3 要求业务身份、局部状态、采集阶段和页型解耦；可信分类/到达应能更新共享图。

**代码事实：**

- [web_page_type_assessment.py](D:/project/Python/alioth/services/api/app/application/web_page_type_assessment.py:276) 把 `capture.web_capture_stage` 写入 `page_states`。
- [web_capture_evidence_repository.py](D:/project/Python/alioth/services/api/app/infrastructure/db/web_capture_evidence_repository.py:136) 将其写为 Capture 的 `page_state`，常见值是 `web_page_ready_baseline` 或 `web_primary_content_evidence`。
- [page_graph.py](D:/project/Python/alioth/services/api/app/domain/exploration/page_graph.py:981) 的 URL 业务身份条件需要 `stable/normal/loaded/content_available` 等状态。
- Web 装配在 [web_runtime_composition.py](D:/project/Python/alioth/services/api/app/application/web_runtime_composition.py:898) 关闭 logical surface/target 路径；单独写入 `logical_page_id` 也不等于共享图消费了该身份。
- 初次 Capture 建图的 [page_capture_repository.py](D:/project/Python/alioth/services/api/app/infrastructure/db/page_capture_repository.py:273) 使用 `page_state=None`；Web 专用分类持久化 SQL 没有补做图刷新。

**离线复现：** 同一合法 URL、reachable、recognized、置信度 0.99，传两个 Web stage 时均返回 `identity=unknown`、`counts_as_business_page=False`；仅把状态换为 `stable` 后才 confirmed。

**影响：** 报告按 URL 聚合出逻辑页，不代表业务图已经建成。Web 的可达路径、多入口、状态图展示与恢复基础不能判定完成。

**调整方向：** 分离 `capture_stage`、业务状态与业务身份；在可信评估接纳后通过共享图入口追加/刷新投影，保持 Coverage 权威不变。不能仅把所有 stage 加进“稳定状态”白名单。

### F03 · P1：Hash SPA 的不同路由被合并，详情入口可能直接消失

**设计要求：** 02 的路由/局部状态语义，以及 08 Task 6 的 SPA 导航和准确去重。

**代码事实：**

- [web_desktop_navigation.py](D:/project/Python/alioth/services/api/app/application/web_desktop_navigation.py:520) 构造候选 URL 时丢弃 fragment。
- [web_logical_page.py](D:/project/Python/alioth/services/api/app/application/web_logical_page.py:39) 的规范化也丢弃 fragment。
- [web_detail_candidate_extraction.py](D:/project/Python/alioth/services/api/app/application/web_detail_candidate_extraction.py:129) 排除同文档链接；其比较不区分不同业务 Hash 路由。

**离线复现：**

```text
https://example.test/#/list
https://example.test/#/detail/1
→ 候选 URL 均为 https://example.test/
→ logical_page_id 相同
→ 构造 home/list/detail 菜单后，list/detail 被并入 home
→ /app#/list 中合法 /app#/detail/1 卡片的详情候选数为 0
```

**影响：** “重复 URL 为 0”可能只是把不同页面错误合并，不能作为去重正确的充分证明。也会把业务 Hash 路由误解为根路径入口。

**调整方向：** 统一 Web 导航、菜单、详情、报告和图的 URL 语义，区分普通页内锚点与业务 Hash 路由；同 URL 的局部筛选状态进入状态 revision，不全部拆页，也不全部丢弃。

### F04 · P1：主体补图生产链在父链与采集阶段处断开

**设计要求：** 07、08 要求保留首屏、自然滚动、追加主体证据、再分类及报告主截图选择。

**代码已经装配主体服务**，见 [web_runtime_composition.py](D:/project/Python/alioth/services/api/app/application/web_runtime_composition.py:984)。问题不是缺少类，而是以下接口不一致：

1. [web_primary_content_capture.py](D:/project/Python/alioth/services/api/app/application/web_primary_content_capture.py:191) 把旧发现帧作为 `direct_parent_capture_id`；真实 Coordinator 会新建动作前帧，after 的直接父亲是新 before，见 [capture_coordinator.py](D:/project/Python/alioth/services/api/app/application/capture_coordinator.py:905)。仓储 [web_capture_evidence_repository.py](D:/project/Python/alioth/services/api/app/infrastructure/db/web_capture_evidence_repository.py:1065) 精确验证直接父亲，二者不一致。
2. 主体 intent 只提供 `capture_purpose`，没有 `web_capture_stage`，见 [web_primary_content_capture.py](D:/project/Python/alioth/services/api/app/application/web_primary_content_capture.py:407)。Coordinator 的 [stage 白名单](D:/project/Python/alioth/services/api/app/application/capture_coordinator.py:1484) 也没有主体补图阶段。
3. stage 缺失会使底层不生成 Web readiness；[read_ready](D:/project/Python/alioth/services/api/app/application/web_runtime_evidence.py:111) 随后报 `WEB_CAPTURE_READINESS_MISSING`。
4. 主体调用 [异常分支](D:/project/Python/alioth/services/api/app/application/web_runtime_composition.py:550) 返回 `None`，上层保留首屏结果。

**影响：** 自然滚动和文件截图可能已经发生，但主体标记、再分类和正式证据闭环不能完成。“截图文件存在”与“主体补图成功”不是同一状态。

**调整方向：** 同时保留“最初基线父证据”和“动作直接 before 父证据”；完整传递并校验主体采集 stage/readiness；补真实 Coordinator→Writer→仓储→read_ready→分类→报告集成测试，并把失败原因显式返回。

### F05 · P2：多候选主体的选择器串位

[web_primary_content_capture.py](D:/project/Python/alioth/services/api/app/application/web_primary_content_capture.py:294) 解包的是 `_selector`，但构造对象时传入上一循环残留的 `selector`。

两条 article 候选的离线结果：第一名为 `fp-a`，选择器却为 `#b`，后续按指纹和 selector 同时复验返回 `None`。

**调整方向：** 逐候选绑定其自身选择器；加入“多候选、得分顺序与 DOM 顺序不同”的测试。该问题本身改动小，但会影响 F04 的端到端验收。

### F06 · P2：广告覆盖率没有按真实视口计算

[web_primary_content_capture.py](D:/project/Python/alioth/services/api/app/application/web_primary_content_capture.py:359) 直接累加广告矩形面积，没有视口裁剪或重叠去重。生产 Snapshot 可包含离屏元素，不能把 CSS 可见等同于视口内曝光。

离线复现：视口 1000×800，1000×800 广告已滚到 `y=-1000`，覆盖率仍为 **1.0**；同一块 1000×200 广告的父子节点被分别统计，结果 **0.5**，实际覆盖为 **0.25**。

**影响：** 即使业务主体已经露出，仍可能超过 0.35 阈值而被判主体补图失败。

**调整方向：** 计算广告可见矩形与视口交集的并集面积，处理父子/重叠区域与未知几何；不得通过隐藏广告修正指标。测试须覆盖离屏、部分离屏、重叠与嵌套，不能只注入预设覆盖率。

### F07 · P2：未观察到产品能力被写成“确认不存在”

04 明确规定“未发现≠不存在”。但 [web_product_capability_assessment.py](D:/project/Python/alioth/services/api/app/application/web_product_capability_assessment.py:122) 在稳定帧、结构计数全零时返回 `confirmed_absent` / `business_capability_absent`，没有受信声明或明确负向验证。

离线已复现。Canvas、自绘、结构采集缺口或当前页面没有该入口，都可能触发这种错误解释。生产 Recorder 会调用此评估。

**已核实的保护：** 没有发现该值在当前链路进一步缩小冻结的 required 分母；本报告不将其夸大成已发生覆盖率虚增。

**调整方向：** 默认输出 unknown，只有受信负向事实才能 confirmed_absent；保留能力来源与修订。在后续接规则适用性时继续禁止未知自动变成不适用。

### F08 · P2：详情导航在就绪检查前就宣告可信到达

[web_list_detail_navigation.py](D:/project/Python/alioth/services/api/app/application/web_list_detail_navigation.py:442) 根据 URL 匹配填充后置结果，`after_content_identity=None`；随后才执行 readiness。即使 readiness 进入验证码/弹窗/加载阻断，[失败分支](D:/project/Python/alioth/services/api/app/application/web_list_detail_navigation.py:473) 仍携带此前 `transition_arrived=True` 的 outcome。

**已核实的保护：** 失败分支同时返回 `detail_capture_id=None`、`page_type=None`，不会正常完成后续分类。因此当前确定问题是“可信到达”审计语义错误，不等于最终覆盖必然增加。

**调整方向：** URL 变化只能证明部分 effect；业务后置与身份/可见层/readiness 完成后才产生 arrival。菜单、滚动、关闭也应复用同一四层合同，而非只有详情导航具有该结构。

### F09 · P2：白名单外候选只有记录，没有低风险探索

01 要求有限自由发现；08 Task 6 Step 4 明确允许未命中项记录并低风险探索。

目前 [web_menu_exploration.py](D:/project/Python/alioth/services/api/app/application/web_menu_exploration.py:409) 的执行集合来自 Catalog selected entries；新候选只进入 `runtime_discovered_ledger`。[web_site_menu_registry.py](D:/project/Python/alioth/services/api/app/domain/page/web_site_menu_registry.py:26) 强制 `record_only_needs_review` 策略。

**影响：** 能识别 button/SPA/无 href 候选，不代表能探索新增菜单。站点菜单发生变化时，已有白名单仍是执行边界而不只是辅助优先级。

**调整方向：** 在原 Runtime/Gate 下加入独立、有界、低风险的发现队列，与必检、恢复队列共享任务总预算并预留收口时间。不得把新候选直接当正式类型或可信到达。

### F10 · P2：六站验收脚本仍混淆合规收口与全量无缺口成功

08 Task 9 允许明确的 blocked、needs_review 和证据不足，要求诚实记录，而不要求所有网站所有目标都成功。

但 [web_v2_six_site_shadow.py](D:/project/Python/alioth/scripts/web_v2_six_site_shadow.py:1675) 对所有站点要求类型待复核数和人工待确认数均为 0，逐页必须 classified；[聚合校验](D:/project/Python/alioth/scripts/web_v2_six_site_shadow.py:899) 还按 Catalog 固定预期执行、首页与详情数量。

这些条件适合“全成功目标”，不能单独决定“阻断处理和 preview 实现是否符合设计”。真实验证码或充分记录的未知结果，不应自动被等同为代码失败。

**调整方向：** 分别输出：执行与台账完整性、设计契约通过、目标实际覆盖、人工复核、服务端 Gate、正式报告绑定；保留总体严格验收结论，但区分未满足原因。不得为获得绿色结论放宽正式 Gate 或强行分类。

## 五、七个专项的完整符合度

| 专项 | 判断 | 已落地部分 | 仍需补齐 |
| --- | --- | --- | --- |
| 01 受控探索 | 部分满足 | 唯一 Session 门禁、命令复验、Capture 前后链、持久预算、锁版 CLI 与命令约束、确定性 Skill 加载 | 非白名单发现、必检/恢复/自由发现统一调度、完整成本/恢复预算与真实灰度验收 |
| 02 页面状态图 | 共享能力已有，Web 接线不足 | 共享图分离观察/身份/状态，支持多入口、循环、可信边、Overlay 分流、同 Run/Epoch 与只读投影 | F02、F03；Web 局部 Tab/筛选/登录状态接入；Web 自身图/Coverage 对账与图树验收 |
| 03 精准补采 | 完整能力未实现；本轮关闭符合计划 | Web 主动补证请求明确转 evidence_insufficient，共享 enabled/runtime_ready 门禁保留 | 图选路、逐边独立恢复、四种声明、新 InputSet/重判完整闭环；必须另行立项启用 |
| 04 页面识别 | 部分满足 | 四类闭集、六项指南字段、结构与截图输入、强反证、复核出口、独立能力/入口/层枚举 | F07；按 Run 的持久版本围栏；能力修订→适用计划失效；规则逐位/素材/交互频控与复用审计 |
| 05 截图就绪 | 部分满足，存在误放 | DOM/loader/媒体/网络信号、连续等待、身份前后快照、显式状态 | F01；角色专属 profile、完整采样窗口与动作后置、网络能力分级、动态时间证据 |
| 06 精准交互 | 部分满足 | 当前候选复验、唯一性与 hit-test、详情滚动后读取新 Capture、年龄/验证码分流、部分弹窗/外跳恢复 | F08；所有动作统一四层结果；每个实际滚动子步骤门禁；回源对象/状态等价，而不只是 URL；真实坐标/浏览器矩阵 |
| 07 可信证据 | 基础链完整，语义门禁部分满足 | Capture/Artifact、Manifest、Publication/Outbox、InputSet、同运行围栏、诊断隔离入口、报告画廊 | F04～F06；合法失败时间序列；对象/可见层/时间/用途约束贯穿所有 Writer 和消费者；长页派生与模型实际读取来源展示 |

### 5.1 不能把实例内版本缓存当成 Run 冻结

[WebPageClassifier](D:/project/Python/alioth/services/api/app/application/web_page_classification.py:290) 持有目录实例并写入 guide_version，这是正确基础；[默认目录加载](D:/project/Python/alioth/services/api/app/domain/page/web_page_catalog.py:31) 是进程级无参数缓存。

生产重新构建 Runtime 读取当前目录，没有按 Run 指定旧目录摘要/指南版本的完整读取合同。当前只能证明一个存活实例内一致，不能证明同 Run 重启、恢复、部署升级后仍使用原指南。该项为实现机制缺失，未进行实际升级故障演练。

指南字段非空检查也不等于设计中的发布期语义编译、正反证矛盾检查、编译器/校验器版本与缓存失效围栏。

### 5.2 产品能力修订尚未驱动完整规则计划

[revise_web_page_applicability](D:/project/Python/alioth/services/api/app/application/web_page_applicability.py:47) 保留冻结分母是正确的，但未发现生产调用；[任务洞察](D:/project/Python/alioth/services/api/app/application/task_insight.py:874) 当前展示的是正式页面能力并集。

已有聚合 RuleScopePlan/InputSet 不是 04 新增的整套频控策略。尚未发现 Web 接通“素材静态可复用、位置逐位检验、关闭按 occurrence、账号/素材/展示层变化使复用失效”的完整闭环。应列设计未完成项，不能在缺少实际证据时直接断言现有规则已经漏检。

### 5.3 正式证据不能只在入口收紧、消费者仍按文件完整性判断

目前 Web admission 与通知门禁已有保护，不应概括成“没有证据校验”。但共享 [EvidenceCompletenessAdjudicator](D:/project/Python/alioth/services/api/app/application/evidence_completeness.py:83) 主要判断附件类型、数量、采集角色和字节数；[InputSet 封存](D:/project/Python/alioth/services/api/app/domain/evidence/input_set.py:369) 主要核对任务、Manifest 与 Capture role。

[Web readiness](D:/project/Python/alioth/services/api/app/application/web_readiness_contracts.py:127) 对各角色统一要求 stable；[Observation Packet](D:/project/Python/alioth/services/api/app/application/observation_packet.py:810) 对 loading/transitioning 的拒绝路径仍存在。因此 07 所要求的合法加载失败、关闭失败时间序列不能算已经完整接入。

另需继续核对“分类→通知”结果闭环：[分类包装器](D:/project/Python/alioth/services/api/app/application/web_page_inspection_notification.py:699) 未消费通知 blocked 返回值。后续 reconciliation 虽有补偿，仍须保证每页明确记录未发布原因；不能把 classified 显示成已经完成质检。

### 5.4 工作台：报告入口基本具备，详细排障投影仍不足

已核实：

- [任务列表](D:/project/Python/alioth/app/workstation/src/modules/tasks/pages/TaskListView.vue:168) 有 preview 标签和 none 原因。
- [任务详情](D:/project/Python/alioth/app/workstation/src/modules/tasks/pages/TaskDetailView.vue:252) 有 Web 专属报告可用性逻辑。
- [报告中心](D:/project/Python/alioth/app/workstation/src/modules/reports/pages/ReportCenterView.vue:756) 展示 preview、质检进度、类型复核、Gate 缺口与逻辑页画廊。
- [报告 API](D:/project/Python/alioth/services/api/app/api/v1/workstation/reports.py:141) 区分读取异常；[审计错误](D:/project/Python/alioth/services/api/app/api/v1/workstation/read_errors.py:19) 保留可追踪错误编号。
- [正式报告读模型](D:/project/Python/alioth/services/api/app/application/report_insight.py:1093) 消费封存逻辑页，而不是任意最新 Capture。

因此，**不能继续将当前状态概括为“没有报告页面或按钮”**。代码和页面测试已证明入口存在。但本轮没有逐个打开当前六站任务，不能保证实际后端部署、权限、数据与路由组合全部无问题。

距离 02、04～07 的完整工作台设计，还缺统一展示：readiness 分层缺口、指南修订/反证、能力不存在依据、四层动作结果、坐标与来源追溯、规则未执行/复用失效原因。已有图片列表和报告统计不等于完整诊断界面。

## 六、对 08 实施计划逐项结算

| 任务 | 当前判断 | 收口条件 |
| --- | --- | --- |
| Task 0 验收基线 | 离线基线已有，真实矩阵需校正 | 补 Hash SPA、真实父链、动态误放和允许阻断的基线；区分契约验收与全成功目标 |
| Task 1 页面就绪 | 部分完成，不可验收关闭 | 修复 F01，补角色、连续窗口、网络与时间一致性跨层测试 |
| Task 2 精准交互 | 部分完成 | 修复 F08，统一各动作结果，验证同 URL 状态变化与逐步滚动 |
| Task 3 图与证据门禁 | 部分完成 | 修复 F02，贯通真实 Writer/分类/图/通知及主体补图 |
| Task 4 四类指南 | 主要分类逻辑已有，版本治理未完成 | 固定 Run 版本与摘要，补发布语义验证和真实混淆样本 |
| Task 5 能力/入口/展示层 | 部分完成 | 修复 F07，接入能力修订和规则适用性解释；不改冻结分母 |
| Task 6 导航发现 | 候选扩展已有，探索调度未完成 | 修复 F03，实现 F09，对每个未知/跳过/执行目标保存终态 |
| Task 7 Web Skill | 代码层基本满足 | 已确定性注入、不授权 shell/JS；行为效果仍需真实流程验证 |
| Task 8 报告可见性 | 代码与页面测试基本满足 | 当前部署下从任务列表/详情分别打开六站报告并复核所有状态 |
| Task 9 六站回归 | 未完成最终验收 | 新版本真实六站运行、逐站人工报告复核、服务端 Gate 和最终回归材料 |

**精准补采关闭不应被误判为遗漏修复。** [Web 主动交互边界](D:/project/Python/alioth/services/api/app/application/web_active_interaction_boundary.py:28) 明确将需要主动动作的 Judge 请求结算为证据不足；这是 08 §6 要求的保护。必须在前置能力验收后另起计划，不应为消除当前缺口直接打开。

## 七、本次验证与不能据此宣称的结果

### 7.1 本次新执行的测试

| 验证组 | 结果 | 说明 |
| --- | --- | --- |
| 全部 `services/api/tests/test_web_*.py` | **1203 passed / 49 skipped** | 本次现行工作区，22.27 秒；跳过不计通过 |
| 报告 API/任务洞察/正式报告/六站样本/跨端相关 8 文件 | **215 passed / 5 skipped** | 与上组存在重叠，不能相加当作独立总数 |
| 工作台任务列表、详情、报告中心、报告详情 4 文件 | **39 passed** | 页面组件和状态矩阵，不是六站真实 UI 人工复核 |
| 受控探索/图独立审查 4 文件 | **81 passed** | 覆盖现有受限实现，不覆盖新增自由发现或 Web 图刷新 |
| 类型/能力/主体证据独立审查 6 文件 | **69 passed** | 主体测试使用简化 Coordinator/Writer，未覆盖真实父链 |
| 就绪/交互独立审查 6 文件 | **94 passed / 12 skipped** | 其中浏览器实测因本机 Chromium 不可用跳过 |

后端统一使用仓库 `.venv/Scripts/python.exe -B -m pytest ... -q -p no:cacheprovider`；前端运行四个相应 Vitest 文件。本次未重新执行完整前端生产构建，不将计划中历史构建结果包装为本次新验证。

完整 Web 命令：

```powershell
$webAuditTests = @(Get-ChildItem -LiteralPath 'services/api/tests' -Filter 'test_web_*.py' | ForEach-Object { $_.FullName })
& 'D:/project/Python/alioth/.venv/Scripts/python.exe' -B -m pytest @webAuditTests -q -p no:cacheprovider
```

额外八文件为 `test_workstation_reports_api.py`、`test_task_insight_api.py`、`test_web_v2_formal_report_e2e.py`、`test_web_final_report_binding.py`、`test_web_six_site_shadow_fixtures.py`、`test_worker_h5_v2_route.py`、`test_h5_exploration_workflow.py`、`test_app_exploration_mode_parity.py`。

### 7.2 离线复现与集成推导分开

- **已实际纯函数复现：** dynamic→stable 正式准入、Web stage→图身份 unknown、Hash SPA 合并/详情丢失、主体 selector 串位、广告离屏与重叠误计、能力 confirmed_absent。
- **基于实际生产接口逐层追踪确认：** 主体补图直接父链不匹配、stage/readiness 缺失、到达审计先于 readiness。
- **未进行本次真实验证：** 浏览器六站、第三方模型调用、真实数据库下所有 Writer 的故障恢复、目录升级后同 Run 恢复、逐站人工报告核对。

### 7.3 历史六站材料不能作为当前版本验收

现有 [2026-09-07 六站回归报告](2026-09-07-web-v2-six-site-dev2-regression.md) 明确记录 **未通过**：模型服务额度阻断导致后续五站在判定前失败，六站均为 preview；还存在首页/详情证据与 Frontier 缺口。

该材料可用于排障及证明失败报告存在，不证明当前代码的六站探索、质检和正式报告已经通过。08 Task 9 也仍保留真实运行、人工复核和最终报告待完成项。

本次是代码符合度审查，没有恢复此前真实任务，也没有重新触发网页证据向外部模型服务发送。

## 八、建议收口顺序与修改规模

| 顺序 | 工作包 | 涉及核心文件 | 相对规模 | 必须新增的验收 |
| --- | --- | --- | --- | --- |
| 1 | 修正就绪误升级 | `page_capture.py`、`web_readiness_contracts.py`、`web_evidence_admission.py`、`web_runtime_evidence.py` | 中；跨采集与准入 | 未完成 quorum/关键请求不得成为稳定正式证据 |
| 2 | 打通主体补图 | `web_primary_content_capture.py`、`capture_coordinator.py`、`web_capture_evidence_repository.py`、`web_runtime_composition.py` | 中；单点修复小，集成验证较大 | 真实 before/after 父链、stage、再分类、主图；离屏/重叠广告 |
| 3 | 统一身份、图与 SPA 去重 | `web_logical_page.py`、`web_desktop_navigation.py`、`web_detail_candidate_extraction.py`、`web_page_type_assessment.py`、图投影/仓储 | 中到大；共享身份契约 | Hash 路由分离、局部状态 revision、分类后业务图刷新、无重复主记录 |
| 4 | 校正四层动作与能力解释 | `web_list_detail_navigation.py`、`web_interaction_precision.py`、`web_product_capability_assessment.py`、`web_page_applicability.py` | 中 | 阻断不得 arrival；未发现能力保留 unknown；分母不变 |
| 5 | 补指南与规则证据合同 | Catalog/Classifier、Manifest/InputSet/Observation Packet、Publication/规则执行相关模块 | 大；需要跨模块评审 | Run 固定版本、合法失败序列、诊断隔离、修订与复用失效 |
| 6 | 接入有限自由发现 | `web_menu_exploration.py`、registry、Runtime、预算 ledger | 中到大；行为范围变化 | 未列菜单只在同源低风险预算内执行，收口预算不被耗尽 |
| 7 | 补工作台诊断和验收口径 | task/report insight、任务/报告组件、六站脚本 | 中 | 可解释未知/阻断、证据来源、错误编号；契约与覆盖分别结算 |
| 8 | 最终回归 | 上述集成用例、数据库/浏览器用例、六站真实任务及人工复核 | 独立验收阶段 | Task/Run/Epoch、每页类型/质检、截图、菜单终态、报告入口与 Gate 对账 |

规模是耦合程度判断，不是精确工时。前四包优先解决当前正确性和主链断点；完整七项设计还包含较大的共享证据与调度工程，不能按几处补丁估算为全部完成。

实现时优先复用既有 JSON/快照与共享服务。是否需要新增持久版本或状态关系字段，应先设计再评审迁移，不在修复中隐式扩展数据模型。修复可复用问题后再同步专项回归规则；本次审查不提前修改规则或计划勾选。

## 九、建议的最终验收条件

1. 动态/超时/错帧/遮挡不会因转换或兼容分支获得稳定自然证据资格。
2. 每个正式逻辑页都有类型结果与明确质检状态；needs_review 有记录和理由，不是无记录丢失。
3. Hash 业务路由正确区分，同一业务逻辑页多来源不重复正式采集；明确追加观察保留独立历史。
4. 主体补图能贯穿真实 Coordinator、仓储、readiness、分类、Publication 与报告，而非只验证服务桩。
5. 命令、效果、后置和可信到达四层分别可解释；未知副作用不盲重试。
6. 图投影、Coverage、规则输入与报告使用同 Run/Epoch 的合法事实，类型改判不分裂身份、不修改冻结分母。
7. 从任务列表与任务详情均能打开同一任务 preview/final；正式导出必须对应服务端 Gate 与封存绑定。
8. 六站逐站保存 Task ID、Run ID、菜单终态、分类/质检分布、阻断、原图与主体图、报告链接和人工核对记录。
9. 跳过用例、外部额度/环境阻断、产品失败、证据不足与代码缺陷分别报告；不能相互替代。

**最终判断：可以继续在现有架构上收口，但当前不能标记“全部设计完成”或“六站验收通过”。建议先修正确认的主链缺陷，再补设计广度，最后做真实验收；正式精准补采继续保持关闭。**
