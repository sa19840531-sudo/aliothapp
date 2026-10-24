# 执行裁定汇总（持续更新）

计划：`docs/superpowers/plans/2026-09-09-web-design-gap-remediation.md`。

截至最终限定复审完成，汇总 progress.md 中全部36条 `Ruling:`。按实施阶段及各阶段记录顺序排列；原台账将部分最新记录前置，本表保留原文及来源位置，不改写历史。36条原文/锚点/代价已核验，复审没有新增裁定；本表是执行决策清单，不替代[交付结论](delivery-summary.md)和[限定复审](final-fix1-review.md)，也不表示六站目标验收完成。

## 1

原计划“本次只写文档”已被新执行授权替代；本轮选择保留未提交差异，不自动提交或推送。此前对话存在 git add/commit 授权，本条不是声称用户撤回了历史授权；当前请求以 Web 优化、审查和验证为交付重点。

代价：本轮结果仍依赖当前工作树与留存差异包，未形成可直接推送的提交。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:89)

## 2

只改 Web 专属逻辑及必要的共享入口 Web 分支，不改 H5/App 行为，不升级公共依赖 — 用户限定范围 — 若共享层无法隔离则该子项须报告，不放宽所有平台门禁。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:90)

## 3

执行简报和审查记录放 docs/reports/2026-09-09-web-gap-execution；T 编号用本地解析提取，不用只支持 Task 编号的技能脚本 — 遵循项目所有文档放 docs 和现有计划结构 — 成本为手动生成每项简报与差异包。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:91)

## 4

T1 同时修复 Web bundle 对持久化 admission 布尔值的盲信，并让 Web bundle 查询返回 pipeline_metrics — 否则旧误放事实仍可参与新 bundle 构造 — 增加两个 Web 文件的审查面，不重写历史封存报告。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:146)

## 5

T3 dismissed_layers 只计已证实消失的目标层；同 selector 内容变化不算关闭成功 — 保证动作尝试、效果、后置与业务到达分别如实结算 — 原有前后帧和动作 outcomes 保留，允许原预算内复验候选后继续清障，不增加重试额度。

代价：缺少可靠消失证据时按未知处理，可能增加合法预算内的复验。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:147)

## 6

T5 发布 web-url-identity.v2；旧 v1 在当前实现明确 unsupported 失败关闭，不改写冻结正文或封存报告，不沿用 v1 标签暗换语义。该兼容策略须在测试/报告说明旧 Run 不能继续按新语义采集，不能把版本阻断宣称探索成功。

代价：旧 v1 Run 不可直接继续采集，需要明确版本阻断并按新策略创建任务。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:148)

## 7

T5 现有分类表仅按 Capture/schema 唯一，无法原子占有跨 Capture 目标或约束别名；允许最小 Web 专属 qc_web_logical_target / qc_web_logical_target_alias 和0101迁移（实际head须复核），先写实体/关系/状态契约，再TDD与隔离PG验证。claimed/materialized仅是占有/材料状态，不代替published/judged；别名只依目标请求到真实落地的可信跳转或声明，不能把点击前源页误并到点击后目标页。优先摘要+Capture引用，避免重复保存敏感URL；主库与其他端不变。

代价：增加两张 Web 事实表及迁移、并发和恢复验证面，错误别名绑定会损坏去重身份。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:149)

## 8

T8 新发现统一消费当前密封候选的真实CLICK；Web bundle仅在实际Web首跳哨兵、同Run/Session/Epoch、唯一首动作、空白before与真实after直接父链得到证明时接受无requested父节点 — 从零探索必经这两处，不能推迟后仅凭detail_captured宣称链路通过 — 扩大本项Web读侧/入口负向回归面；若判断错误可能误接纳伪首跳，必须测试普通动作缺requested、伪H5哨兵、跨Run、非首动作及错误parent拒绝，不放宽后续父链或T6图准入。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:181)

## 9

不为补记脚本前置检查而重复同版本完整132项回归 — 已有最终退出0与目录新建/精确清理事实，原缺漏需如实归档并补未来命令检查 — 不能将事后核对冒充事前验证；若额外组已启动则让其有界结束和清理，不再重开重复组。主控下一次新鲜回归仍显式检查新目录。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:194)

## 10

T8 I1 复修采用明确的Web菜单收口拒绝结果，真实current/owner/open和预算行锁下仅结算未领取目录，再持久结算已观察未执行发现 — None/异常/已有reservation混为预算拒绝会误关闭未知动作或丢owner状态 — 成本为Web预算端口返回合同同步与真实PG负向回归；已领取/终态/复用、分母/槽位原样，不新增迁移，收口未覆盖不能宣称产品通过。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:203)

## 11

批准T9报告方案的最小Web实体扩展：Web序列/成员及能力修订事实、原Publication严格stable/sequence二选一、每规则成员InputSet绑定、明确web_sequence scope — 未分类失败无合法recognized FK，旧PAGE单Capture和多规则Work不能承载该材料而保持真实语义 — 成本为必要共享表/合同的Web分支兼容迁移及实库跨端拒绝回归；不建新队列、不放宽旧scope/H5/App/stable，不动主库，有审计事实时下迁移拒绝删除。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:218)

## 12

T9无分类target时允许序列按可信Capture/真实目标与冻结T5身份独立持久target摘要，不伪造分类目标；动作before帧只证明触发链，不计目标持续失败样本数/时长 — 导航前空白或源页不证明目标持续加载 — 成本为区分动作闭包与同目标采样的测试面。每规则InputSet进入新分支工作哈希/Packet缓存，旧无绑定哈希及缓存键不变；同anchor不同规则的阈值/材料不得复用第一条Packet。required和最终Gate不变，能力absence限定有证据逻辑页/状态。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:219)

## 13

T9 首批可信负向仅支持完整可枚举当前文档/视口的闭集原生结构命题，且只供显式同作用域规则消费；产品能力继续区分未知，不据局部零计数或目录缺失宣称整站不存在 — 现有运行时无产品级受信负向声明来源，不能凭空补造 — 若后续需要更广产品级否定，需另补受控声明来源与权限/版本审核，不能用本项局部证明替代。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:55)

## 14

真实 Chrome、模型及 HTTP 端到端统一留给 T10；T9 验证实际 prepare/Coordinator/PG/正式消费与三种 Manifest 装配，不重复建立全套浏览器验收环境 — 按原计划 C 阶段消费修复、D 阶段集成验收的职责划分 — 浏览器采集与新消费之间的集成风险仍待 T10 关闭，不把 T9 局部替身测试当作真实浏览器通过。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:56)

## 15

local_structure 首批消费限定显式 snapshot/llm 序列合同，普通 stable/一般业务规则不自动启用 — 以现有显式规则权限与封存 InputSet 接通受信局部修订，避免更改旧频率或用局部命题跳过一般规则 — 不能据此称所有 stable 页面或产品级能力闭环已全量启用，未实际实施的静态素材复用须单列，不以计划hash代替功能验收。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:57)

## 16

首批规则复用接入已有有界语义缓存，只允许显式 same_material_context、同一认证封存InputSet及完整语义上下文等价 — 同一材料重复消费可省模型调用，但不能改写位置/关闭实例频率或证据来源 — 不增加跨平台缓存；跨Capture/跨页面素材复用仍不启用，交付须单列此设计剩余范围，保留原频率，不宣称全部静态复用完成。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:58)

## 17

I1/I4 必须在 Web 新入口以当前真实消费者租约及账号事实认证，不以 Writer 拒写或 H5 配置替代 — 首审证明现有正例漏掉可达未授权消费 — 若边界过严可能使旧缺身份调用显式不足，但不得为兼容而放行；H5 旧流程不变。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:75)

## 18

I2 未完成分类不能永久不适用；I3 不同完整冻结合同不等价，原 Attempt 和来源必须保留 — 与任务冻结/可审计合同一致 — 最小重试及不兼容终态方案先由原实现者提出，再记录接线裁定，防止无限等待或改写冻结材料。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:76)

## 19

T9 I2 使用原 Publication 的 retry_wait 与 Outbox 等待同锚点分类，不改序列/schema；仅经认证的 classification_pending 不消耗技术故障预算，受原冻结任务期限及收口终态约束，分类已结束仍未知则 needs_review — 分类事件和序列消费异步是合法顺序，不能把先后竞态判成不适用 — 若错误识别 pending 可增加延迟，故缺少可信期限/持续授权时须明确不足而非无限等待；重试锁定原每规则完整合同hash，漂移不重开终态、不造页型。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:78)

## 20

T9 I3 对当前及历史来源认证后发现完整合同不兼容时，保留旧canonical与全部原始Attempt，经Web专属异常使当前原Work/Attempt明确技术失败收口，不进入通用重试 — 不同标准不能隐式并成同一个质检结果，且原队列必须终结 — 当前用途不能产生可合并正式结论，后续报告须区分原始判断与合同不兼容错误；不得伪装产品fail，也不能覆盖已写成员或历史原答。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:79)

## 21

I3 技术不兼容终结改为Web专属、同Task锁事务的仓储端口，不放宽原transition_rule要求 — 实库已拒跨hash，但旧通用服务要求error有rule_execution_id，本案不能借旧A结果绑定B；原共享表与finalize_task_work已有无结果成员error形状 — 若专属认证不足会产生旁路，故须重验真实序列、当前Work/Attempt/consumer/lease、已持久原决策与实际A/B合同差异，原子保留payload并终结未写成员和Work；无新迁移/结果表。冷恢复及最终finalize_task_work同样不得把旧canonical回填当前合同，须有定点验证。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:81)

## 22

T9 N1 允许最小传播已认证 Web 专属批次合同异常到原 Consumer 技术终结，保留普通 H5/App 批次故障隔离；同时验证真实先写后冲突的单条审计来源，不能简单放宽失败 Attempt 状态认证 — 实际批次被通用异常处理截断，原单条测试漏掉该形状 — 若传播或历史来源准入过宽会影响其他端/认证边界，需精确源绑定及普通错误回归；详细要求在第二轮简报。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:29)

## 23

N1 终结前复用原 sync_web_projection 认证并绑定当前已提交来源，再仅终结未写成员；terminal_error历史来源必须由已写成员→正式execution→精确canonical来源及原decision/Packet/完整合同共同证明 — 真正逐条Writer提交早于批次member同步，不可手工置written模拟 — 若认证不完整会放行未写失败来源，故新增负例并保持Task/Run/Epoch/租约围栏；不新建来源表、不全局扩大Attempt状态准入。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:31)

## 24

N1反向批次可达（sequence仅B、当前stable同category含A+B）；允许仅在当前真实Work成员集合确有同Task/producer/Epoch/anchor/session受认证序列用途时，将该Work其余成员纳入逐条来源投影 — 原_scopes只含B会漏掉A实际先提交的结果 — 若扩大成同任务任意Work有序列即准入则会越界，故保留成员集合约束、逐条完整身份并测无关Work负例；普通A独立Work/H5/App不变，已有普通canonical来源路径需核对实际唯一性或明确认证，不能借旧结果。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:34)

## 25

Work唯一键含input_hash，批准当前受认证A+B回溯普通历史A的精确canonical←member.execution_id←已written Attempt来源，保留原Web资格、同Task/producer/Epoch/anchor/rule和完整合同/Packet认证 — 底层publish可产生不同冻结批次，不能用Publication唯一性假定历史不存在 — 若回溯范围过宽可能借旧合同或旧控制模式，须真实不同input_hash路径及缺/伪绑定负例；旧独立Work不扩入全局scope、合同不兼容仍拒合并、查无行明确受控拒绝。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:35)

## 26

历史不同合同用例不得为进入Writer而放宽冻结Packet门禁；仅有合法完整合同变化且材料定义不变时测试投影，否则按实际早期拒绝/零模型/旧canonical不变断言并单列未到投影 — 现有安全校验先拒绝不是允许伪造身份的理由 — 若未到投影意味着该子形状投影未单独验收，保留该边界；原两用途实际Writer跨合同矩阵仍需保持，不把替代路径混称同一验证。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:39)

## 27

T10 验证进程增加精确测试库出站白名单，所有实际 asyncpg 连接/建池入口在认证前拒绝默认主库回退；报告目录中的本轮runner作为可执行支撑文件纳入审查和摘要 — 旧测试DI遗漏已多次导致guard身份认证失败，只有正确注入还不足以防止下一处漏接 — 代价是只允许专用55439环境运行该runner，其他合法测试环境需显式重新审查配置；不改变生产连接逻辑，也不将被拦截测试计为通过。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:12)

## 28

为验证真实浏览器到新消费，允许仅本轮独立测试API/Worker进程显式注入同版中性测试Catalog，并通过真实机制冻结该测试版本 — 正式Runtime在浏览器启动前精确registry.resolve_site，localhost/example.test没有发布目录，默认拒绝是合法边界而非浏览器故障 — 代价是目录与中性页面仍属替身，不能证明六站实际覆盖或默认生产Catalog全链；不修改全局六站目录、不假标版本、不关闭身份/权限/Gate、不伪造外站成人声明，新增执行支撑必须纳入审查，数据库/队列/附件/角色及端口独立隔离。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:25)

## 29

T10 纳入真实严格 Web 离场门禁自锁修复：增加显式 current_payload 技术需求身份/作用域，与分类后的业务需求分离；真实对象校验、版本化 Manifest 与精确需求修订共同收口，旧 Writer 拒绝新 Web 技术类型，Web 专属重裁不得借旧 PASS 满足新增规则 — 实采证明 Web readiness 关闭发生在分类/需求发布之前，默认 off 掩盖生产 on 的 pending 清单阻断；四页型全集可能要求互斥/未来证据，空候选无条件 pass 或伪 rule_id 都不可用 — 成本为本项扩入 Web 技术合同/仓储/Writer/组合根及兼容测试，增加迁移与并发审查风险；先定义实体/权限/接口，不默认新增表或修改主库，旧 H5/App/v1 序列化和门禁默认配置不变，未稳定/unknown/required覆盖不因此升级。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:241)

## 30

T10 真实关闭后发现 T9 交互选样将 bootstrap 无前驱误作普通动作来源；仅允许经既有可信 Web 首跳、同 Task/Run/Session/Epoch 与实际首动作验证的初始采集不计入后续关闭动作样本，保留源 Capture 和原始材料，随后 close/observe 闭包继续严格校验 — 旧受控夹具给初始采集普通父源，漏掉真实首跳形状；str(None) 不能成为来源 UUID — 成本为 Web 序列登记跨项追加修复及伪哨兵/非首跳/跨 Run/普通缺父链负例，不能泛化为忽略缺父帧、伪造 UUID 或吞异常，T9 已批准的其他合同仍保留并由最终跨项审查复核。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:243)

## 31

T10 实采序列超过48KB语义事实预算时，允许修复已认证不足投影丢失 InputSet/完整规则合同绑定的 Web 早退分支；保留预算和 evidence_insufficient 原因，仅在真实消费与材料绑定已验证后携带同一来源，并核对该函数同类已认证早退 — 原不足结果无逐规则绑定被 Writer 围栏拒绝，实际路径不能如实落库 — 成本为T9消费投影跨项回归；认证失败不能补造绑定或信任调用者自报，短中性正常通路与真实大材料不足单列，不能缩小测试正文后宣称大材料已判定成功。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:245)

## 32

T10 保留旧 stable 默认读取合同，采用真实 Capture/Artifact ID、实际对象 hash 与同 Capture 的 PASS Manifest 分层交叉核验，不伪称 Packet 已持久绑定 Manifest ID 或序列 InputSet；另在测试隔离范围沿既有公开 shadow 对账→Resolver on 稳定读取组合验证新技术 Manifest 兼容性，若既有接口不能无歧义注入则记录具体边界，不改写旧 stable 业务 — 最终真实链发现默认 Manifest read off 的普通 stable Packet 本就没有这些绑定，强加序列断言会把兼容验收变成合同迁移 — 成本为旧默认材料认证仍只能证明同源对账而非冻结 Manifest-ID 绑定，需在交付与独立审查显式保留；不得伪造字段、全局切 read on、降低交互/预算/权限负例的逐成员严格绑定。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:248)

## 33

T10 I1 复修复用同 Task/Run 的既有 web_coverage 冻结快照，required/实际 observed 交集/missing 按页型计数；菜单账本只提供 menu_entry 单位的阻断/排除数量，已观察质检队列另列，不混加不同实体分母。缺快照、来源身份或集合一致性为 unverified；完成须冻结目标兑现且无待复核、待判定或执行内阻断 — 原快照没有菜单级完成/阻断字段，但现有两类权威事实足以如实分层，无需新建后端合同 — 成本为报告同时出现页型、菜单和队列三个单位，消费者必须读取单位且不能把任一零值当整体完成；目录当前配置不得替换 Run 冻结值，预算跳过/跨域排除不缩 required。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:251)

## 34

T10 I2 正式链实库暴露共享最终门禁把合法 Web 失败序列仍按 stable qualified 库存比较，允许最小 Web 专属分支修复：真实 Web V2、同 Task/当前 finalization Run/Epoch、精确序列/锚点/来源成立后，以既有被选择成员的 InputSet、Manifest/冻结合同和 Work 关联一致性替代不适用的稳定库存计数，其他批次、成员、孤立 Work、终态检查不跳过；合法零候选/不适用保持原选择合同 — 实测 register→Publication published→Work succeeded→Attempt written 与消费者 done 后仍因 NULL vs 1 永久阻断，分层描述不能冒称失败序列已正式封存 — 成本为跨项新增共享收口入口的 Web 窄分支，错误认证可能提前封存，必须实库覆盖缺/伪绑定、错 Run/Epoch/anchor、未终态/孤立 Work，以及普通 Web stable/H5 原拒绝；无新增迁移、主库操作或 required 缩减。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:255)

## 35

最终修复 I3 允许在既有 Web local_structure selection 中追加发布时实际传入的 rule_contract_hash、rule_version 与完整闭集 declaration，供最终 Gate 认证同一冻结局部声明、规则合同和 capability revision/来源；不在最终化阶段用当前可变规则重算旧声明，也不扩三个共享收口入口 — 当前 NA 无 InputSet/Work，旧 selection 仅保存状态、revision 和 reuse_key，缺少可验证的原规则声明，字符串放行不能满足审查要求 — 成本为旧已存在且缺少冻结声明的 local_absent 记录仍须受控拒绝/新 Run 重验，不能宣称历史积压自动恢复；不回填冻结审计或报告、不增加表/迁移。新增嵌套声明须避免缓存浅拷贝共享可变对象，并验证声明/hash/版本/来源篡改与旧格式拒绝。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:271)

## 36

最终修复 I3 允许同一 Web selection 冻结 plan 已生成的原 canonical identity JSON 正文，以原 UTF-8 字节摘要校验既有 reuse_key，并将解析字段与新增声明/版本/hash及独立持久 revision、序列、规则列和来源逐项对照；不在 SQL 复制 Python canonical JSON 算法 — 旧 reuse_key 已覆盖这些原输入，但只追加版本字段无法验证其单独篡改 — 成本为少量重复持久数据及解析兼容面；哈希自洽不等于授权，仍须原 Task/Run/Epoch/规则/材料围栏，缺失、非法 JSON、错误类型或越界数据应受控拒绝而非触发最终 Gate 500，不放大原预算、不新建材料合同/迁移，旧缺正文记录继续新 Run 重验。

[原台账记录](D:/project/Python/alioth/docs/reports/2026-09-09-web-gap-execution/progress.md:272)
