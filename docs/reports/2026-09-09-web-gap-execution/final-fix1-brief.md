# 最终审查单次修复波简报（已核对完整反馈，准备派发）

final-review.md已定稿，主控已完整阅读，最终审查者STOP。恢复原web_gap_t10执行本次唯一修复波，再由原最终审查者限定复审；不派子代理、不改其他业务。仅处理以下三项Important，M1～M4按最终审查为非阻断Minor继续记录，不扩展本波。

最终审查原文标题：I1 / P1「默认 Runtime 构建必经的局部名称未绑定」；I2 / P2「完整 live 验收聚合未消费实际覆盖维度，能输出总体通过」；I3 / P1「R34 最终序列 Gate 拒绝生产可生成的 local_absent 不适用成员」。完整原文、精确行与探针见final-review.md的Important部分，三项全部读取后开始。

## 输入与基线

- 需求：docs/superpowers/plans/2026-09-09-web-design-gap-remediation.md 全部Global Constraints；此前34裁定继续绑定，不因最终修复缩减required或放宽授权/封存/父链。
- 完整反馈：同目录final-review.md（定稿后为发现的权威原文）。本简报的三项是主控已实读核对的根因摘要，不能代替完整反馈。
- 精确复修前全文：HEAD 968566da87d0fecbb13c6abebae13d684e656341 叠加 final-review-fix1-package.md；251范围及LF SHA在final-review-fix1-scope.json，全部磁盘重放见final-review-fix1-validation.json。不是HEAD..HEAD、不是T10起点；不覆盖旧包。
- 唯一实现报告：docs/reports/2026-09-09-web-gap-execution/final-fix1-report.md。完整记录RED→修复→GREEN、命令/退出/失败原值、替身/实库层级、净文件清单、清理与STOP。

## 三项已核对根因与最小验收面

### 默认Runtime构建

web_runtime_composition.py的build在1080先用build_evidence_artifact_store，在1135才函数内导入，Python把该名视作全函数局部变量；未注入assessment_agent路径会UnboundLocalError，包括mode off。现有两composition用例注入assessor，未覆盖默认构建。

最小修复作用域/复用既有工厂，不改变H5/App或关闭新Web门禁。test_web_runtime_composition.py新增或扩展不注入assessor的实际build回归，覆盖off/shadow/on；同时复用test_web_run_policy_snapshot_postgres.py:183默认assessor冻结/冷恢复测试，该测试只替换模型客户端/Agent，不代表仓库完全无默认路径测试。允许隔离网络/存储/数据库边界，不准给provider注入替身assessment_agent或只做AST断言绕过失败路径。先确认当前默认路径RED，再修复并复验原Web组合根与该默认PG路径；不要借此重构大文件。

### Live总体验收误报

LiveSixSiteReport.passed目前只取live_validation_passed、manual_report_review_passed与server_completion_gate_passed，actual_coverage的incomplete/unverified不参与；原全局Gate合法允许completed_with_gaps，所以正式绑定不能代替覆盖兑现。真实--live输出JSON/Markdown/HTML及退出码使用此属性。无--live的Catalog静态预检独立按drift_count判断，不应因缺实站事实永久失败。

在既有六维合同上明确live总体通过要求、缺失/未执行状态及失败诊断，不再建立第二套覆盖计算。覆盖完整、缺失冻结required、无可信coverage、待复核/待判定、人工未验收/未绑定、明确选站子集与离线静态路径；本轮全用现有内存requester/受控夹具，不访问六站，不伪造成人声明或实际人工记录。不能仅把总数>0或服务端Gate=true当总体完成，不修改后端Gate为拒绝所有合法completed_with_gaps。

### local_absent不适用到最终序列Gate

生产web_local_structure.py:118～121对已认证local_absent生成not_applicable/WEB_LOCAL_NATIVE_STRUCTURE_LOCAL_ABSENT，sequence processor:174～219读取同Task/Run/Epoch/Capture revision、核对序列revision并冻结selection，:328可合法not_applicable；web_sequence_completion_gate_sql.py:133～138闭集漏该生产原因，导致active_or_inconsistent。真实能力PG原用例只到Consumer/Writer，未检查最终Gate。

最小接通现有合法合同：认证同一冻结capability revision、scope、predicate、规则合同及来源后允许该明确原因，不仅把字符串加入白名单，也不接受任意reason/not_applicable、伪造InputSet/Work、改变unknown/insufficient语义或缩required。复用test_web_capability_revision_postgres.py和test_web_sequence_completion_gate_postgres.py/必要正式测试支撑，走实际revision→生产selection→Publication→原最终Gate，NA-only和NA+admissible两类正例到实际finalization，必要时验证正式绑定。使用真实合法序列用途和局部原生结构证明，不为测试将失败/未稳定材料标stable。补伪造/未知/跨Run及缺乏可信局部证明的拒绝或不足边界，并保留其他门禁/原跨平台条件；不复制另一套消费者或材料合同。

## 执行与测试约束

1. 先完整读final-review.md、核对三项，按默认build→live聚合→local_absent收口逐项RED/GREEN。Minor是否处理以最终报告及主控派发为准，不为依赖告警/历史长行升级或全仓格式化。
2. 纯测试用项目.venv；PG只允许已有127.0.0.1:55439 / alioth_test_web_v2 / alioth_web_gap，迁移20260910_0103。继续复用task-10-test-runner.py的实际connect/create_pool白名单、内存秘密与有界超时。主库、共享服务、六站均不操作，禁止默认认证。PG在本次正式派发后独占交给实现者，原最终审查者未连接PG。
3. 不重跑全部Web、共享482/旧广组或真实中性浏览器组；只跑上述改动覆盖文件和明确关联最小组。每次失败保留原结果，不累加重叠组。若需新增用例之外的大范围或新的公共接口，先反馈具体证据。
4. 只用apply_patch改源码/文档，中文注释UTF-8；同步专项规则本次复用价值条目。保留用户原dirty，不git add/commit/push、不主库迁移/重启。
5. 清理仅准确自有本轮Task/关联实体及已核验绝对路径临时目录，不操作他人实体、不宽泛删除或停未知进程。报告不可原地恢复的临时产物及验证限制。无须为OS枚举权限失败扩大权限。
6. 完成自审、限定fresh回归后源/测试/格式/文档ALL STOP、PG RELEASED。主控生成相对此最终审查全文的净包，原最终审查者只复查本轮发现与新破坏；仅一次修复波与一次复审，禁止自行循环扩任务。

## Ruling35：I3冻结声明最小补齐

最终修复 I3 允许在既有 Web local_structure selection 中追加发布时实际传入的 rule_contract_hash、rule_version 与完整闭集 declaration，供最终 Gate 认证同一冻结局部声明、规则合同和 capability revision/来源；不在最终化阶段用当前可变规则重算旧声明，也不扩三个共享收口入口 — 当前 NA 无 InputSet/Work，旧 selection 仅保存状态、revision 和 reuse_key，缺少可验证的原规则声明，字符串放行不能满足审查要求 — 成本为旧已存在且缺少冻结声明的 local_absent 记录仍须受控拒绝/新 Run 重验，不能宣称历史积压自动恢复；不回填冻结审计或报告、不增加表/迁移。新增嵌套声明须避免缓存浅拷贝共享可变对象，并验证声明/hash/版本/来源篡改与旧格式拒绝。

## Ruling36：原canonical identity正文绑定

最终修复 I3 允许同一 Web selection 冻结 plan 已生成的原 canonical identity JSON 正文，以原 UTF-8 字节摘要校验既有 reuse_key，并将解析字段与新增声明/版本/hash及独立持久 revision、序列、规则列和来源逐项对照；不在 SQL 复制 Python canonical JSON 算法 — 旧 reuse_key 已覆盖这些原输入，但只追加版本字段无法验证其单独篡改 — 成本为少量重复持久数据及解析兼容面；哈希自洽不等于授权，仍须原 Task/Run/Epoch/规则/材料围栏，缺失、非法 JSON、错误类型或越界数据应受控拒绝而非触发最终 Gate 500，不放大原预算、不新建材料合同/迁移，旧缺正文记录继续新 Run 重验。
