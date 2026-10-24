# Web 探索质检六类设计缺口修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复已审查确认的就绪、页面图、SPA 身份、主体补图、受控发现和设计闭环缺口，使结论能由同一 Run 的可信证据逐层证明。

**Architecture:** 保留现有 Web Runtime、Capture Coordinator、分类器、图投影、Publication/InputSet 与报告 Gate，只修正事实合同及其传递。身份、页面类型、就绪状态、采集阶段、证据用途分别存储；新事实追加修订，不覆盖原始证据和已封存报告。低风险发现仍由现有 Runtime 单一持有者执行，不增加另一套浏览器执行器。

**Tech Stack:** Python / FastAPI / PostgreSQL / Microsoft Agent Framework；Vite / Vue / TypeScript；pytest / Vitest。

**Spec:** [设计目录](../../design/2026-09-03-exploration-optimization/README.md)、该目录 01～07 专项及 [08 实施计划](../../design/2026-09-03-exploration-optimization/08-web-development-priority-plan.md)；根因证据见 [2026-09-09 代码审查报告](../../reports/2026-09-09-web-exploration-design-compliance-audit.md)。执行每项前同时阅读对应专项，不把本计划当成授权扩大范围。

## Global Constraints

- 本计划已获用户授权实施；仅优化 Web 探索和质检，不改其他业务。当前保留未提交变更，不执行提交或推送；真实六站验收须满足测试环境、安全和证据条件。
- 基线：本地分支 codex/web-v2-qingyunke-development-plan，审查提交 968566da87d0fecbb13c6abebae13d684e656341，包含当前未提交实现。执行时先核对差异并保留用户改动。
- 页面业务类型保持 video_list、video_detail、content_list、content_detail 四类；首页是页面角色，弹窗/广告是可见层，unknown/needs_review 是判定状态，不增加第五种业务类型。
- 任务提交时冻结 required 分母；发现不到、阻断、unknown、能力修订不得直接缩小分母。
- 菜单任务上限保持 10；必检、恢复、自由发现共享现有任务预算和终止期限。额外发现不是“10 个之外再加一批”。
- 保留原始首屏与动作前后证据；只能自然交互、自然滚动补图，不能隐藏广告、改 DOM 后冒充自然证据。
- 同 Run/Epoch/Session、当前动作授权、候选复验、直接父链和封存围栏不得放宽。
- 明确区分已分类、已发布质检、已判定、报告 preview、正式绑定；合法 blocked/needs_review 不是产品通过。
- 本计划不启用 Judge 主动补采，不修改 H5/App 业务流程，不绕过人工验证；共享组件改动必须做跨端回归。
- 使用 UTF-8、中文代码注释。实施每批后同步 docs/专项能力与高频回归规则.md。
- 每个任务先增加根因回归并确认红灯，再最小实现、绿灯、审查。提交仅在执行阶段获得适用授权后进行，逐项暂存，提交说明包含 [AI编程]、模块、迁移、前端路由影响和未覆盖风险。

## 一、交付顺序与调整大小

| 阶段 | 任务 | 调整大小 | 放行条件 |
| --- | --- | --- | --- |
| A：先停止错误结论 | T1 就绪；T2 能力否定；T3 到达语义 | 中 | 未稳定不再升级；未发现不再等于不存在；URL 到达不再等于业务到达 |
| B：身份及证据贯通 | T4 Run 策略冻结；T5 SPA 身份；T6 页面图；T7 主体补图 | 中～大 | 身份版本可恢复，真实 Writer 链和图投影集成通过 |
| C：补齐受控策略与消费 | T8 非目录发现；T9 失败序列与规则适用性 | 大 | 所有动作有预算/终态；不同证据用途不能串用 |
| D：验收与复盘 | T10 报告投影、集成、六站回归 | 中，受环境影响 | 代码合同、真实覆盖、人工复核和正式 Gate 分开验证 |

这是同一条证据链的分批交付，不是重写 Web 探索。T4 的冻结机制必须先于新身份策略在生产 Run 中启用；T8 必须后于 T1/T3/T5/T6。T9 不以放宽 T1 的稳定帧门禁实现。

## 二、文件责任与数据策略

以下路径相对仓库根目录。现有文件列在各任务中；明确标为“新增”的接口和文件均为方案，不声称当前已经存在。

- 采集事实：infrastructure/browser/page_capture.py；就绪纯判定：application/web_readiness_contracts.py；正式准入：web_evidence_admission.py、web_runtime_evidence.py。
- URL/身份：web_logical_page.py、web_desktop_navigation.py、web_detail_candidate_extraction.py；共享业务图仍由 domain/exploration/page_graph.py 及现有图仓储管理。
- 主体补图：web_primary_content_capture.py 负责选择/执行/结果，新增 web_viewport_geometry.py 仅负责几何计算；不把新逻辑继续堆入底层 page_capture.py。
- 冻结策略：新增 domain/exploration/web_run_policy_snapshot.py 和 infrastructure/db/web_run_policy_snapshot_repository.py；新增独立迁移，不复用可覆盖的工作记忆冒充不可变快照。
- 规则证据语义：新增 application/web_rule_evidence_contract.py；共享 InputSet/Observation Packet 仅在 Web 显式合同下扩展消费。
- T1/T2/T3/T5/T6/T7 首选现有字段与版本化 metadata，不为简单修复增加表。T4 明确新增 Run 不可变策略表；T8/T9 先复用现有台账/Manifest 存储，若现有数据库约束不允许新增字段值，提交独立兼容迁移并先做升级/回滚测试，禁止删除约束解决。
- 旧证据缺少新字段时显示历史/证据不足；不得补造 stable 或按当前指南重写历史判定。已封存 InputSet、Coverage、报告不变。旧运行无法恢复旧策略时明确阻断并通过新 Run 重测，不静默套用最新策略。

## 三、可独立验收的实施任务

### T1：修正就绪事实与稳定证据准入

**修改：** services/api/app/infrastructure/browser/page_capture.py；application/web_readiness_contracts.py、web_evidence_admission.py、web_runtime_evidence.py（均在 services/api/app 下）。

**测试：** services/api/tests/test_web_capture_readiness_metrics.py、test_web_readiness_contracts.py、test_web_runtime_evidence.py、test_web_evidence_bundle.py。

**接口：** 扩展现有 WebReadinessSignals，新增 settle_complete、quorum_met、capture_consistent 三个 bool | None 事实和 profile_version: str；由采集者写入、持久化读取者校验。未提供不是 True。采样窗口起止、文档 epoch、关键请求与一致性结果保存在同一份指标中。can_serve_evidence 对稳定分类/稳定质检仍要求 stable；T9 单独处理失败序列。

- [ ] 在 test_web_capture_readiness_metrics.py 增加以下回归，并使用现有判定器消费而不是只检查字典字段。

~~~python
def test_inspectable_dynamic_is_not_stable_evidence():
    from services.api.app.application.web_readiness_contracts import read_web_readiness_assessment
    from services.api.app.infrastructure.browser.page_capture import _web_readiness_metrics

    metrics = _web_readiness_metrics(
        web_capture_stage="web_page_ready_baseline",
        identity_before="https://example.test/list",
        identity_after="https://example.test/list",
        page_settle_complete=False,
        page_inspection_ready=True,
        readiness={"core_present": True, "visible_loader": False,
                   "transition_active": False, "necessary_media_ready": True,
                   "dom_quiet_ms": 1200},
        network_observable=True,
        critical_network_pending=1,
    )
    assessment = read_web_readiness_assessment({"web_readiness": metrics})
    assert assessment is not None
    assert assessment.status.value != "stable"
    assert assessment.can_serve_evidence is False
~~~

- [ ] 执行：从仓库根目录运行 .venv/Scripts/python.exe -B -m pytest services/api/tests/test_web_capture_readiness_metrics.py -q -p no:cacheprovider；预期新用例先失败。
- [ ] 在真实等待结果源头分别记录“可观察”和“满足稳定窗口”，删除 inspection_ready 抵消 settle 失败的逻辑。稳定准入必须满足下式，并继续检查 loader、媒体、身份和采样窗口；网络是否必需来自冻结 profile，不能写死 False，也不能等待所有广告/长连接全部结束。

~~~python
stable_window_proven = (
    signals.settle_complete is True
    and signals.quorum_met is True
    and signals.capture_consistent is True
)
~~~

- [ ] 将现有 capture_consistency 的文档/窗口/时间事实接入，禁止仅靠 URL 相等认定截图与结构同一 epoch。等待中、超时、人工阻断分别保留原因。
- [ ] 对上述四个测试文件执行 pytest；新增网络未知但规则要求网络、同 URL 跨文档、采样不足、结构变化及真正稳定的正例。验收要求：失败材料仍可留存 preview/诊断，但不能入稳定正式 bundle 或可信业务图。
- [ ] 审查后记录本任务差异和剩余跳过项；未授权不提交。

### T2：取消“结构计数为零即确认不存在”

**修改：** services/api/app/application/web_product_capability_assessment.py、web_page_type_assessment.py、web_page_applicability.py。

**测试：** services/api/tests/test_web_product_capability_assessment.py、test_web_page_type_assessment.py、test_web_page_applicability.py。

**接口：** 现有 WebProductCapabilityAssessment.entry_status 无充分负向证据时返回 UNKNOWN。确认不存在必须按具体能力记录 trusted negative proof 的来源 Capture/声明、作用页面、版本和观察时间；不能从单页推断整个网站。能力修订消费在 T9 接入。

- [ ] 增加零结构、Canvas/自绘、弹窗遮挡三个回归。零结构最小断言：

~~~python
def test_zero_structure_is_unknown_not_absent():
    from services.api.app.application.web_product_capability_assessment import assess_web_product_capabilities

    result = assess_web_product_capabilities(
        readiness_status="stable", business_visible=True,
        navigation_element_count=0, repeated_card_count=0, video_element_count=0,
        article_body_count=0, advertisement_element_count=0, transaction_element_count=0,
    )
    assert result.entry_status.value == "unknown"
    assert "business_capability_absent" not in result.reason_codes
~~~

- [ ] 执行 .venv/Scripts/python.exe -B -m pytest services/api/tests/test_web_product_capability_assessment.py -q -p no:cacheprovider，确认旧逻辑失败。
- [ ] 无负向证明的分支改为以下语义；保留已存在的 visible_layer 阻断优先级。

~~~python
entry_status = WebEntryStatus.KNOWN if capabilities else WebEntryStatus.UNKNOWN
reason_codes = () if capabilities else ("business_capability_not_observed",)
~~~

- [ ] 运行三个测试文件并断言 frozen required 前后完全相等；对已有错误历史状态不执行批量覆盖。
- [ ] 审查区分“已纠正零结构错误”与“T9 的完整适用性修订尚未完成”；单独形成可验收变更。

### T3：动作回执、效果、后置与可信到达重新分层

**修改：** services/api/app/application/web_interaction_precision.py、web_list_detail_navigation.py、web_menu_exploration.py、web_primary_content_capture.py；逐项核对 web_page_readiness.py 中关闭/恢复调用。

**测试：** services/api/tests/test_web_interaction_precision.py、test_web_list_detail_navigation.py、test_web_menu_exploration.py、test_web_primary_content_capture.py。

**接口：** 现有 assess_web_interaction_outcome 新增 readiness_accepted: bool = False、identity_verified: bool = False、business_layer_visible: bool = False 关键字参数；默认失败关闭。after_content_identity 来自实际 after，不再固定 None。各动作按自身后置合同提供 effect/postcondition 事实，URL 变化仅是其中一种观测。

- [ ] 复制现有详情导航成功 fixture，分别替换 after 为验证码、持续 loading、同 URL 但目标正文切换；前两例 command_accepted 可为 True，transition_arrived 必须为 False，第三例满足完整条件时为 True。
- [ ] 在现有交互测试上增加同样的参数矩阵，先执行 .venv/Scripts/python.exe -B -m pytest services/api/tests/test_web_interaction_precision.py services/api/tests/test_web_list_detail_navigation.py -q -p no:cacheprovider，确认暴露现有提前到达问题。
- [ ] 先记录命令与实际效果，再完成 readiness、自然可见层、目标身份及后置验证，最后生成最终 outcome。保留失败前已发生动作事实，不把所有字段一起重置。结果满足：

~~~python
transition_arrived = bool(
    command_accepted and effect_observed and postcondition_satisfied
    and after_capture_id and readiness_accepted and identity_verified
    and business_layer_visible
    and evidence_mode is WebInteractionEvidenceMode.NATURAL
)
~~~

- [ ] 菜单、详情、每次实际滚动、关闭/回源逐一迁移调用，关闭使用层消失与底页状态恢复，滚动使用目标曝光，不用 URL 变化强套所有动作。旧 Capture、失去 lease、诊断后帧不得通过。
- [ ] 运行四组测试；审查所有 assess_web_interaction_outcome 调用点，确保没有靠默认值掩盖未迁移行为；再形成单独审查记录。

### T4：冻结 Run 的指南与身份策略

**新增：** services/api/app/domain/exploration/web_run_policy_snapshot.py；services/api/app/infrastructure/db/web_run_policy_snapshot_repository.py；services/api/migrations/versions/20260909_web_run_policy_snapshot.py；services/api/tests/test_web_run_policy_snapshot.py、test_web_run_policy_snapshot_postgres.py。

**修改：** services/api/app/domain/page/web_page_catalog.py；application/web_page_classification.py、web_runtime_composition.py；Web Run 初始化/恢复的调用点。

**接口（新增）：** WebRunPolicySnapshot 使用不可变 dataclass，字段为 task_id: str、agent_run_id: str、payload: Mapping[str, object]、payload_sha256: str。payload 必含完整页面目录/指南、菜单先验、guide_version、validator_version、identity_policy_version、readiness_profile_version、discovery_policy 和预算；不能只有版本名。仓储 freeze_or_read(snapshot: WebRunPolicySnapshot) -> WebRunPolicySnapshot 原子首次冻结；read(*, task_id: str, agent_run_id: str) -> WebRunPolicySnapshot | None 只读恢复。

- [ ] 增加 PostgreSQL 测试：Run A 首次冻结 V1，目录改 V2 后冷启动仍读 V1；新 Run B 读 V2；并发初始化只产生一条一致快照；篡改摘要失败。
- [ ] 先运行两个新文件确认冻结机制缺失；新增迁移前用仓库 Alembic 命令确认唯一 head，设置真实 down_revision，禁止生成新的未合并分叉。
- [ ] 新表以 agent_run_id 为主键并约束 task 归属；首次运行任何分类/动作前插入全量 payload，冲突读取既有值，禁止 UPDATE 覆盖。摘要使用规范 JSON 的 SHA-256：

~~~python
canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
payload_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
~~~

- [ ] 工厂/恢复显式注入冻结目录与配置；无旧正文、摘要不符或不支持版本时终止当前 Run 并记录原因，禁止回退进程缓存。发布校验检查四类闭集、正反证规则标识冲突、易混淆类型引用及必需字段；语义歧义保留发布审查而不是声称静态校验能理解全部自然语言。
- [ ] 新增测试验证冻结值不被进程重启、工作记忆更新或封存动作修改；执行迁移升级/回滚和 web_page_classification、web_runtime_composition 回归。数据库测试跳过不算本任务通过。
- [ ] 独立审查数据库权限、不可变性、旧 Run 兼容和存储大小；提交说明必须注明引入迁移。

### T5：统一 SPA URL 身份与跨入口去重

**修改：** services/api/app/application/web_logical_page.py、web_desktop_navigation.py、web_detail_candidate_extraction.py、web_menu_exploration.py；与 services/api/app/domain/exploration/page_graph.py 的 Hash/状态划分对齐。

**测试：** services/api/tests/test_web_logical_page.py、test_web_desktop_navigation.py、test_web_list_detail_navigation.py、test_web_menu_exploration.py。

**接口：** 保留 canonical_web_logical_page_url(url: str) -> str 等现有外观，生产调用必须从 T4 绑定身份策略版本；路由型 fragment 是身份的一部分，普通锚点不是。HTTP/HTTPS 或域名跳转是否为别名必须由受信跳转/站点策略证明，不能借本次改动新增无条件合并。

- [ ] 先增加以下用例，并用同一 URL 组喂给菜单、详情提取和报告聚合。

~~~python
def test_hash_routes_are_distinct_and_anchors_are_not():
    from services.api.app.application.web_logical_page import canonical_web_logical_page_url as key

    assert key("https://example.test/#/list") != key("https://example.test/#/detail/1")
    assert key("https://example.test/#/detail/1") != key("https://example.test/#/detail/2")
    assert key("https://example.test/article/1#comments") == key("https://example.test/article/1")
~~~

- [ ] 执行 .venv/Scripts/python.exe -B -m pytest services/api/tests/test_web_logical_page.py -q -p no:cacheprovider，确认旧逻辑把 Hash 路由合并。
- [ ] 统一保留 #/...、#!/... 的路由片段及其业务参数；普通 #comments 只记录观察位置。无法确定的 Hash 进入结构/路由事实复核，不一律丢弃或一律新增页。_same_web_document 必须区分“同文档”与“同业务目标”，不能因为共享 document 就过滤 SPA 详情。
- [ ] URL path 为空且无业务 Hash 路由时可标 home 角色；业务类型仍看内容。Tab/筛选作为页面状态或业务身份由共享策略决定，不自建与图不同的规则。
- [ ] 在当前 Run 台账统一记录规范键、实际落地别名及正式证据状态；同一逻辑页只一条质检主记录。已完成目标再遇到时不重新分类/发布/采主证据；未知跳转允许必要 after 证明确实重复，追加动作观察但不追加第二条质检主记录。版本化规则不重写封存报告。
- [ ] 运行四组测试，覆盖 hash query、百分号编码、重复标签、入口与详情撞 URL、导航后重定向别名和任务恢复幂等，再审查。

### T6：接通 Web 业务图身份与投影刷新

**修改：** services/api/app/application/web_page_type_assessment.py、web_runtime_composition.py；services/api/app/infrastructure/db/web_capture_evidence_repository.py、page_capture_repository.py、page_graph_repository.py；必要的共享图输入合同位于 domain/exploration/page_graph.py。

**测试：** services/api/tests/test_web_page_type_assessment.py、test_page_graph_projection.py、test_page_graph_run_scope_postgres.py；新增 services/api/tests/test_web_page_graph_projection_postgres.py。

**接口：** 复用现有 project_capture_on_connection，不新建第二套图。web_capture_stage 保留采集阶段；page_state 由 T1 就绪和可见层事实生成。URL 身份按 T5/T4 策略映射；page_type 来自分类修订，不能反向伪造身份可信。

- [ ] 增加真实入库链测试：初始 Capture → ready → 分类写回 → 图查询。稳定业务页应为可信业务节点；类型仍待复核但身份已证实的页面不得仅因类型未知丢掉身份；验证码/广告层仍为非业务观察。
- [ ] 执行新增 PostgreSQL 文件及现有 projection 文件，确认当前 stage 误写 page_state、分类后未刷新能被用例捕获。
- [ ] 分类记录与 Capture 写回同事务刷新图，使用当前 task/run/session/epoch/capture/revision；失败整体回滚。最小状态映射如下，非稳定原因另存完整 readiness，不用阶段字符串替代：

~~~python
page_state = "stable" if readiness.status.value == "stable" else readiness.status.value
~~~

- [ ] 图刷新仍需校验业务可见层、自然来源和身份；上述状态映射不是单独准入条件。禁止为了消除 unknown 把所有页面强行标 stable，也不直接打开 H5 的所有 logical_surface 开关。
- [ ] 测试重复分类回调无重复节点/边、跨 Run/旧 Epoch 写回拒绝、图与 Coverage 同 Run 对账；报告使用图投影不替代 Gate。历史重建仅消费已有受信事实，不补造身份。
- [ ] 四组测试和数据库故障回滚通过后审查；缺少实际 PostgreSQL 结果不得以 fake repository 代替验收。

### T7：修复主体补图的选择、几何与完整证据链

**修改：** services/api/app/application/web_primary_content_capture.py、capture_coordinator.py、web_runtime_composition.py、web_runtime_evidence.py；services/api/app/infrastructure/browser/page_capture.py；services/api/app/infrastructure/db/web_capture_evidence_repository.py。

**新增：** services/api/app/application/web_viewport_geometry.py（仅几何算法）；services/api/tests/test_web_primary_content_capture_postgres.py。

**测试：** services/api/tests/test_web_primary_content_capture.py、test_capture_coordinator.py、test_web_runtime_evidence.py 及新增 PG 测试。

**接口：** viewport_ad_coverage(ui_snapshot: Mapping[str, object]) -> float | None；未知几何返回 None，所有调用点显式分流，WebPrimaryContentTarget.viewport_ad_coverage、Result.before_ad_coverage 相应允许 None。baseline parent 与 direct action parent 分别保留，不改变 after Capture 的真实直接父亲。

- [ ] 在现有 _element fixture 增加以下断言；补几何样本：视口 1000×800、广告 1000×800 位于 y=-1000 时为 0；两条重叠 1000×200 广告为 0.25；缺 viewport 为 None。

~~~python
def test_each_primary_target_keeps_its_own_selector():
    targets = build_web_primary_content_targets(
        capture_id="capture-1", expected_page_type="content_detail",
        ui_snapshot={"viewport": {"width": 1000, "height": 800}, "elements": [
            _element("#a", is_repeated_card=False, contains_primary_media=True),
            _element("#b", is_repeated_card=False),
        ]},
    )
    assert {item.dom_fingerprint: item.selector for item in targets} == {"#a": "#a", "#b": "#b"}
~~~

- [ ] 先运行 test_web_primary_content_capture.py，确认 selector 串位及几何误算；新增 PG 用例必须使用真实 Coordinator/Writer 而非只伪造 after id。
- [ ] 修复排序解包后的 selector 绑定。广告覆盖率按“可见广告矩形与视口的交集之并集 / 视口面积”计算，统一 CSS 坐标、滚动偏移与 viewport 原点；不加总重叠矩形。预计算值仅接受可信采集者同 Capture、同算法版本的指标，不能让任意快照字段绕过校验。
- [ ] 初始 baseline 保存 parent_capture_id；metadata 的 direct_parent_capture_id 取本次 Coordinator 实际 before Capture，必须与 after.parent_capture_id 相等。保留仓储严格检查。
- [ ] 从 intent → Coordinator stage 白名单 → collector → metadata → read_ready 完整传递 web_primary_content_evidence 阶段并按 T1 生成 readiness。每次滚动重新校验候选、授权和目标可见性，广告占比下降不能单独证明已露出业务主体。
- [ ] after 就绪、身份一致、自然呈现且目标可见后才选择主体图、追加分类修订。异常返回明确 reason_code、基线/动作 Capture 与阶段，不吞成无解释 None；首屏保留。同逻辑页不新增质检主行。
- [ ] 验收真实链：baseline → fresh before → after → metadata Writer → read_ready → 再分类 → report 主图；同时测试两次滚动耗尽、覆盖率未知、目标漂移、新弹窗和父链伪造拒绝，再审查。

### T8：将目录外候选接入有界低风险探索

**修改：** services/api/app/application/web_menu_exploration.py、web_runtime_composition.py、web_exploration_budget_ledger.py；services/api/app/domain/page/web_site_menu_registry.py；services/api/app/infrastructure/db/web_exploration_budget_ledger_repository.py。

**测试：** services/api/tests/test_web_menu_exploration.py、test_web_runtime_composition.py、test_web_site_menu_registry.py、test_web_exploration_budget_ledger_postgres_integration.py。

**接口：** T4 冻结 discovery_policy，包含 enabled: bool、max_unlisted_menus: int、same_origin_only: bool。候选记录增加 source_kind=catalog/runtime_discovered、来源 Capture、规范身份/动作候选 ID 和跳过原因；已知 href 用 T5 去重，无 href 以当前密封候选复验并在到达后归并。执行复用 claim_menu/reserve_detail 与既有 broker，不创建旁路。

- [ ] 将原有“所有 unlisted 永远 record_only”的测试拆成关闭策略、开启但安全拒绝、开启且许可执行三组。加入菜单总数达到 10 时第 11 个不执行的 PG 测试。
- [ ] 先运行上述测试确认缺少运行时发现执行路径。默认关闭保持兼容，启用仅作用于明确授权站点/任务，不等于开放任意域名爬取。
- [ ] 先必检目标，再在剩余硬预算内处理目录外候选；保留收口/恢复所需预算。提交时可配置自由发现配额，但不挤占 required 冻结目标，也不在运行中重置总预算。完整结果必须包括以下终态语义：

~~~text
目录外同源候选 + 低风险 + 当前候选复验 + 剩余预算 → 现有 Runtime 执行
外域/登录/支付/下载/未授权动作 → 跳过并记录安全原因
任务预算耗尽/收口预留不足 → 跳过并记录预算原因
已完成逻辑页 → 复用既有结果，不重复正式采集
动作结果未知/失去所有权 → 观察或阻断，不自动重复点击
~~~

- [ ] 候选菜单文本与先验类型只帮助排序；真实到达后仍跑 readiness、四类判定、证据发布与质检。新目标不得自动变成 required 或被先验强制判型。
- [ ] 四组测试验证一次 reservation、一次 owner、共享预算、恢复不重置、每个已发现候选有终态；审查后用小范围受控运行启用，不直接全量放开。

### T9：失败时间序列消费与能力修订闭环

**新增：** services/api/app/application/web_rule_evidence_contract.py；services/api/tests/test_web_rule_evidence_contract.py。

**修改：** services/api/app/application/web_readiness_contracts.py、web_evidence_bundle.py、web_page_applicability.py、observation_packet.py、evidence_completeness.py；services/api/app/domain/evidence/input_set.py；Web 规则计划装配与现有 Manifest 读写边界。

**测试：** services/api/tests/test_web_rule_evidence_contract.py、test_web_page_applicability.py、test_web_evidence_bundle.py、test_observation_packet.py、test_evidence_completeness.py、test_evidence_input_set.py。

**新增合同：** WebRuleEvidenceContract 包含 rule_version: str、accepted_roles: tuple[str, ...]、required_artifact_types: tuple[str, ...]、minimum_samples: int、minimum_duration_ms: int、requires_action: bool。证据语义角色明确区分 stable_page、failure_sequence、interaction_sequence、diagnostic；它们不是新增页面类型，也不直接替换底层 before/after Capture role。

- [ ] 先增加消费矩阵：持续加载序列不能作 stable_page；满足冻结规则时间/对象/自然来源要求时可作 failure_sequence；仅一张失败图、缺动作前帧、乱序、跨 Run、跨目标或诊断污染应为 evidence_insufficient。
- [ ] 对当前 observation_packet 的统一 loading 拒绝写回归，执行六组测试，确保先观察到合法失败序列被整体拒绝，而非简单让旧测试变绿。
- [ ] 新合同校验 task/run/session/epoch、目标与 occurrence、单调时间顺序、必要 before/action/after、最少样本/持续时间、文件完整性及自然来源。每条规则读取其冻结合同；未声明失败序列的规则保持拒绝。语义路由为：

~~~text
stable_page       → 稳定曝光/页面结构规则；继续使用 T1 稳定门禁
failure_sequence  → 明确接受该序列的失败规则；不能证明成功到达或稳定曝光
interaction_sequence → 按动作合同判断成功/失败；不能只数附件文件
diagnostic        → 排障；不能伪装自然成功证据
~~~

- [ ] InputSet 封存具体帧、角色、合同版本和校验摘要；消费者复验绑定而非只看文件数。序列有效仅表示可以判定，不自动等于产品失败；材料不足不自动等于通过。
- [ ] 将 T2 能力修订接入适用计划刷新：按逻辑页/状态修订缓存；有依据的不适用保存解释，但 required 分母不变。规则复用键包含规则版本、素材身份、位置、账号状态、可见层、occurrence；静态素材只在规则允许时复用，位置逐位、关闭按 occurrence 复验。不可把所有规则改成“一站一次”。
- [ ] 运行消费与适用性测试；确认未知主动补证仍按现有边界返回 evidence_insufficient，未额外驱动浏览器。共享 InputSet 修改需验证 H5/App 测试后独立审查。

### T10：报告解释、集成验收与六站复盘

**修改：** services/api/app/application/task_insight.py、report_insight.py、web_page_inspection_notification.py；scripts/web_v2_six_site_shadow.py、scripts/web_v2_live_evidence.py；app/workstation/src/modules/tasks/pages/TaskDetailView.vue、modules/reports/pages/ReportCenterView.vue、ReportDetailView.vue（后两个同属 app/workstation/src）。

**测试：** services/api/tests/test_web_page_inspection_notification.py、test_web_six_site_shadow_fixtures.py、test_web_final_report_binding.py、test_workstation_reports_api.py、test_task_insight_api.py、test_web_v2_formal_report_e2e.py；工作台 TaskListView/TaskDetailView/ReportCenterView/ReportDetailView 对应 .test.ts。

**接口：** 延续当前报告可用性与绑定 API，不新建报告入口。增加上述事实的只读解释字段；分类后通知 blocked 必须反馈为“已分类但未发布质检”及原因，不能用分类计数冒充质检完成。

- [ ] 增加页面矩阵：stable 正式页、未稳定 preview、类型待复核、主体补图失败回退、指南不匹配、未发布质检、合法失败序列判定。单元测试应明确不同状态下按钮、主截图、说明与导出权限。
- [ ] 修正六站脚本的验收维度，不再用 needs_review=0 单一条件代表设计合规。输出如下独立结果，并保留总体未通过原因：

~~~text
contract_integrity：动作/身份/证据合同是否成立
actual_coverage：冻结目标实际完成、待复核和阻断数量
ledger_integrity：所有候选与动作是否有终态、去重是否正确
manual_review：逐页人工复核完成情况
server_gate：服务端缺口与门禁结论
report_binding：preview / final、InputSet 与封存快照绑定
~~~

- [ ] 全部 T1～T9 定向测试通过后，执行全体 test_web_*.py、共享图/Coordinator/InputSet 和 H5/App 跨端回归；执行四个前端测试文件及生产构建。记录命令、结果、跳过原因，不累加重叠测试当独立数量。
- [ ] 在授权与环境允许的测试模式下验证本地 API、Worker、PG、浏览器和模型可用，再创建本轮新 Run 做六站探索质检。任一真实外部服务被阻断时停止该依赖路径并明确报告，不用旧报告代替本轮验收。
- [ ] 每站核对首页→菜单→详情→分类→主体补图→发布→判定→报告。重复 URL 一条质检主记录；Hash 详情不丢；最多 10 菜单；每页有判定或明确未判定原因、质检或未质检原因；从任务列表和详情分别打开报告人工复核。
- [ ] 报告同时列“代码合同是否通过”和“真实目标是否全部完成”。六站有阻断可以证明阻断处理合规，但不能据此宣称六站全部通过或正式报告已绑定。
- [ ] 新增本轮回归报告到 docs/reports，更新专项回归规则；复盘剩余缺口。全部验收前不改既有计划中的未完成勾选为完成。

## 四、审查关卡与完成判定

- A 批审查：是否仍有弱信号升级，能力不存在是否有明确证据，动作失败是否错误 arrival。
- B 批审查：相同页面是否只一条主记录，不同 Hash 是否分开，图身份是否独立于类型，真实 Capture 父链/阶段是否完整，旧 Run 是否可复现。
- C 批审查：自由发现是否旁路权限/预算，失败序列是否能污染成功证据，能力与规则缓存是否保留冻结分母。
- D 批审查：最终报告能否反向定位同一 Run 的原始事实；跳过、preview、needs_review 是否被诚实统计。

方案自检覆盖：用户问题 1→T1；2→T6；3→T4/T5；4→T7；5→T8；6→T2/T3/T4/T9；报告解释与最终验收→T10。未纳入本次执行的是设计 03 的主动精准补采及 H5/App 新功能，而非静默宣称全部专项已实现。

**实施选择：** 可以在当前任务逐阶段执行并设置审查检查点；也可在用户明确选择后按独立任务分配子代理。无论采用哪种方式，数据库集成、代码审查及最终六站人工复核都不可省略。
