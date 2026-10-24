# Web V2 Dynamic Exploration and Evidence Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让新 Web V2 任务完全脱离站点/菜单白名单，通过动态同源导航完成探索，为稳定页面保留主截图与全页长图，为广告直接保留 `ad_page` 证据，并修复通用详情分类和 Task/Run 冻结事实不一致。

**Architecture:** 新 Run 只冻结通用 URL 安全策略、页面类型 Catalog 和动态适用性规则；运行时从可信 DOM 生成同源候选。页面证据按 ready 主 Capture、全页 supplemental 子 Capture、正式分类的顺序生成；广告使用隔离标签页并按来源谱系直接签发 evidence-only `ad_page`。

**Tech Stack:** Python 3.12、FastAPI、PostgreSQL JSONB、Microsoft Agent Framework、Playwright、pytest、Vite + Vue + TypeScript。

**Spec:** `docs/superpowers/specs/2026-09-14-web-v2-dynamic-exploration-and-evidence-optimization-design.md`

## Global Constraints

- 仅改变 Web V2；App/H5 的 Catalog、截图、导航、报告和序列化行为必须保持不变。
- 新 Run 不得以域名、菜单路径或站点目录作为导航授权条件。
- 普通导航只允许真实 DOM 中的同源 HTTP(S) `href`；危险动作继续阻断。
- 跨域广告只允许隔离标签页打开，不推进业务页面指针，不继续探索。
- `ad_page` 是 evidence-only 类型；`age_gate`、`ad_page`、`unknown` 不进入业务覆盖率。
- 每个稳定来源 Capture 最多生成一张 `web_full_page_supplemental` 长图。
- 禁止通过 `hl365.com`、`/archives/` 或其他站点/路径特判修复详情分类。
- Task 与 Agent Run 的 `_task_target` 必须完全一致。
- 所有新增注释使用中文，文件保持 UTF-8。
- 每项生产行为先写失败测试并确认 RED，再写最小实现并确认 GREEN。
- 不得提交或覆盖工作区中与本计划无关的用户改动。

---

## File Structure Map

### Domain and contracts

- `services/api/app/domain/page/web_page_type_catalog.json`：发布包含 `ad_page` 的十类 Catalog。
- `services/api/app/domain/page/web_page_knowledge.py`：增加 evidence-only 分组模型。
- `services/api/app/domain/page/web_page_catalog.py`：发布十类常量并保留历史九类/五类恢复。
- `services/api/app/domain/exploration/web_discovery_policy.py`：冻结动态发现和通用安全边界。
- `services/api/app/domain/exploration/web_coverage_snapshot.py`：保存动态适用性状态。

### Runtime and persistence

- `services/api/app/application/web_runtime_composition.py`：新 Run 不再强制装配站点白名单。
- `services/api/app/application/web_desktop_navigation.py`：删除 allowlist 执行门禁，保留安全审计。
- `services/api/app/application/web_page_applicability.py`：动态计算 applicable/covered/not_observed。
- `services/api/app/infrastructure/db/web_full_page_supplemental_claim_repository.py`：允许分类前 ready Capture 领取长图。
- `services/api/app/application/web_full_page_supplemental_capture.py`：保留真实失败原因。
- `services/api/app/application/web_ad_landing_evidence.py`：广告打开成功后直接返回 `ad_page` 事实。
- `services/api/app/infrastructure/browser/capture_runtime.py`：广告 Capture 写入 `capture_purpose=web_ad_page`。
- `services/api/app/application/web_page_classification.py`：通用详情结构规则和 `ad_page` 分类旁路。
- `services/api/app/application/task_submission.py`：所有 Web V2 任务持久化 `_task_target`。

### Query, reporting, and UI

- `services/api/app/infrastructure/db/web_report_page_snapshot.py`：按 capture purpose 区分长图和广告。
- `services/api/app/application/web_report_explanation.py`：报告 `ad_page` 与长图终态。
- `services/api/app/application/workstation_tasks.py`：投影排队和失败原因。
- `app/workstation/src/` 中现有任务详情组件：展示 queued/pending/running 与失败分类；修改前必须避开当前用户删除的 `inspection.api.ts`。

---

### Task 1: 十类 Web Catalog 与历史恢复边界

**Files:**
- Modify: `services/api/app/domain/page/web_page_type_catalog.json`
- Modify: `services/api/app/domain/page/web_page_knowledge.py`
- Modify: `services/api/app/domain/page/web_page_catalog.py`
- Modify: `services/api/app/domain/page/__init__.py`
- Test: `services/api/tests/test_web_page_type_catalog.py`
- Test: `services/api/tests/test_web_run_policy_snapshot.py`

**Interfaces:**
- Produces: `WEB_EVIDENCE_ONLY_PAGE_TYPE_CODES = ("ad_page",)` 和包含十类的 `WEB_PAGE_TYPE_CODES`。
- Preserves: 历史 v8 九类、v5-v7 五类和更早四类冻结恢复。

- [ ] **Step 1: 写十类分组失败测试**

```python
def test_web_catalog_publishes_ad_page_as_evidence_only() -> None:
    catalog = load_web_page_type_catalog()
    assert catalog.page_type_groups.evidence_only == ("ad_page",)
    assert catalog.require_code("ad_page").category == "evidence_only"
    assert "ad_page" not in catalog.page_type_groups.business
```

- [ ] **Step 2: 运行精确测试并确认因缺少 `ad_page` 失败**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_type_catalog.py -q`

- [ ] **Step 3: 发布十类 Catalog 和 evidence-only 强类型分组**

Catalog 中 `ad_page` 必须声明为不可进入覆盖率、不参与普通分类置信度、只允许受信广告来源签发。

- [ ] **Step 4: 保留历史版本适配并运行测试**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_run_policy_snapshot.py -q`

- [ ] **Step 5: 提交本任务**

```text
[AI编程] feat(web): 增加证据型广告页面类型

模块：Web 页面类型 Catalog；数据库迁移：按约束审计决定；前端菜单/路由：不涉及；风险：历史快照恢复。
```

### Task 2: 新 Run 完全移除站点与菜单白名单

**Files:**
- Modify: `services/api/app/domain/page/web_site_menu_registry.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/application/web_desktop_navigation.py`
- Modify: `services/api/app/application/web_runtime_discovery.py`
- Modify: `services/api/app/application/web_run_policy.py`
- Test: `services/api/tests/test_web_site_menu_registry.py`
- Test: `services/api/tests/test_web_desktop_navigation.py`
- Test: `services/api/tests/test_web_runtime_discovery.py`
- Test: `services/api/tests/test_web_policy_creation_boundary.py`

**Interfaces:**
- Produces: 对任意目标 origin 生效的运行时 DOM href 候选。
- Preserves: `WebSiteMenuRegistry` 仅供 legacy frozen Run 恢复。

- [ ] **Step 1: 写未知域名无需目录即可发现并访问同源 href 的失败测试**

```python
def test_new_web_run_executes_safe_same_origin_href_without_site_catalog() -> None:
    candidate = discover_runtime_anchor(
        target_origin="https://new.example",
        href="https://new.example/category/news/",
        is_navigation=True,
    )
    assert candidate.actionability == "actionable"
```

- [ ] **Step 2: 写普通跨域、危险协议和无 href 仍被阻断的测试并确认 RED**

- [ ] **Step 3: 将 allowlist 字段降级为 legacy 审计，不参与新 Run 执行判断**

- [ ] **Step 4: 新策略快照冻结 origin 安全规则，不冻结站点菜单目录**

- [ ] **Step 5: 运行导航和历史恢复测试**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_desktop_navigation.py services/api/tests/test_web_runtime_discovery.py services/api/tests/test_web_policy_creation_boundary.py -q`

- [ ] **Step 6: 提交本任务**

```text
[AI编程] refactor(web): 新任务改为纯运行时同源导航

模块：Web 导航与策略；数据库迁移：否；前端菜单/路由：不涉及；风险：旧 Run 恢复和跨域阻断。
```

### Task 3: 动态适用性与覆盖率

**Files:**
- Modify: `services/api/app/application/web_page_applicability.py`
- Modify: `services/api/app/domain/exploration/web_coverage_snapshot.py`
- Modify: `services/api/app/infrastructure/db/web_coverage_snapshot_row.py`
- Modify: `services/api/app/infrastructure/db/web_coverage_snapshot_repository.py`
- Modify: `services/api/app/application/web_inspection_reconciliation.py`
- Test: `services/api/tests/test_web_page_applicability.py`
- Test: `services/api/tests/test_web_coverage_snapshot.py`
- Test: `services/api/tests/test_web_coverage_snapshot_repository.py`

**Interfaces:**
- Produces: `required/applicable/covered/applicable_uncovered/not_observed/not_applicable` 动态状态。
- Constraint: 分母只包含 `home` 和本 Run 已证明 applicable 的业务类型。

- [ ] **Step 1: 写未知站点只有 home 固定 required 的失败测试**
- [ ] **Step 2: 写发现入口后进入 applicable、访问失败后进入 applicable_uncovered 的测试**
- [ ] **Step 3: 写 `age_gate/ad_page/unknown` 永不进入分母的测试**
- [ ] **Step 4: 实现动态适用性快照与历史 schema 适配**
- [ ] **Step 5: 运行覆盖测试**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_applicability.py services/api/tests/test_web_coverage_snapshot.py services/api/tests/test_web_coverage_snapshot_repository.py -q`

- [ ] **Step 6: 提交本任务**

```text
[AI编程] feat(web): 按运行时证据计算页面适用性

模块：Web 覆盖率；数据库迁移：否，复用 JSONB；前端菜单/路由：不涉及；风险：新旧覆盖分母隔离。
```

### Task 4: 修复全页长图分类前置循环

**Files:**
- Modify: `services/api/app/infrastructure/db/web_full_page_supplemental_claim_repository.py`
- Modify: `services/api/app/application/web_full_page_supplemental_capture.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/infrastructure/db/page_capture_repository.py`
- Test: `services/api/tests/test_web_full_page_supplemental_capture.py`
- Test: `services/api/tests/test_web_full_page_supplemental_postgres.py`

**Interfaces:**
- Produces: 分类前 ready Capture 可领取且终态可查询的 `web_full_page_supplemental` phase。

- [ ] **Step 1: 写没有正式 page_type/admission 但 readiness stable 时 claim 成功的 PostgreSQL 失败测试**
- [ ] **Step 2: 写 loading、非 current、跨 Run、epoch 漂移仍拒绝的测试**
- [ ] **Step 3: 删除 claim 对 `business_page` 和 `can_enter_formal_bundle` 的分类后置依赖**
- [ ] **Step 4: 所有 gate/claim 异常写入明确阶段终态或结构化日志**
- [ ] **Step 5: 验证子 Capture 只有一个 PNG 且设置 `capture_purpose=web_full_page_supplemental`**
- [ ] **Step 6: 运行单元和 PostgreSQL 集成测试**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_full_page_supplemental_capture.py services/api/tests/test_web_full_page_supplemental_postgres.py -q`

- [ ] **Step 7: 提交本任务**

```text
[AI编程] fix(web): 修复全页补图分类前置循环

模块：Web 长图证据；数据库迁移：否；前端菜单/路由：不涉及；风险：Session claim 和幂等终态。
```

### Task 5: 广告隔离打开并直接签发 `ad_page`

**Files:**
- Modify: `services/api/app/application/web_ad_landing_evidence.py`
- Modify: `services/api/app/infrastructure/browser/capture_runtime.py`
- Modify: `services/api/app/application/web_auxiliary_navigation.py`
- Modify: `services/api/app/application/web_menu_exploration.py`
- Modify: `services/api/app/infrastructure/db/web_capture_evidence_repository.py`
- Test: `services/api/tests/test_web_auxiliary_navigation.py`
- Test: `services/api/tests/test_web_menu_exploration.py`
- Test: `services/api/tests/test_web_ad_landing_evidence.py`

**Interfaces:**
- Produces: `WebAdPageReceipt`，包含 `capture_id`、`requested_url`、`final_url`、`page_type="ad_page"`、artifact IDs。

- [ ] **Step 1: 写广告标签页打开成功后直接返回 `ad_page` 的失败测试**
- [ ] **Step 2: 写打开失败只记录 `WEB_AD_PAGE_OPEN_FAILED`、不得生成页面 Capture 的测试**
- [ ] **Step 3: 写广告不进入普通分类、不产生 evidence_insufficient/unknown 的测试**
- [ ] **Step 4: 保存 `capture_purpose=web_ad_page`、`page_type=ad_page` 和来源父 Capture**
- [ ] **Step 5: 验证广告标签页关闭、业务 current Capture 不变且不继续探索**
- [ ] **Step 6: 运行广告和辅助导航测试**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_ad_landing_evidence.py services/api/tests/test_web_auxiliary_navigation.py services/api/tests/test_web_menu_exploration.py -q`

- [ ] **Step 7: 提交本任务**

```text
[AI编程] feat(web): 广告隔离留存为ad_page

模块：Web 广告证据；数据库迁移：按页面类型约束审计决定；前端菜单/路由：不涉及；风险：跨域隔离和来源谱系。
```

### Task 6: 通用 `content_detail` 结构分类

**Files:**
- Modify: `services/api/app/application/web_page_classification.py`
- Modify: `services/api/app/application/web_primary_subject_facts.py`
- Modify: `services/api/app/domain/page/web_page_type_catalog.json`
- Test: `services/api/tests/test_web_page_classification.py`
- Test: `services/api/tests/test_web_v8_primary_subject_capture.py`
- Test: `services/api/tests/test_web_page_rule_capture_regressions.py`

**Interfaces:**
- Produces: 可审计的主内容结构事实和不依赖站点/路径的 `content_detail` 判定。

- [ ] **Step 1: 用通用 `article.post/.post-content` fixture 写当前被降为 unknown 的失败测试**
- [ ] **Step 2: 增加 WordPress/Typecho、自定义 article、图集和漫画详情参数化测试**
- [ ] **Step 3: 写正文内嵌视频仍为 content_detail、主播放器才为 video_detail 的边界测试**
- [ ] **Step 4: 修复正文作用域、文本长度、单一主体和主内容 token 提取**
- [ ] **Step 5: 将结构事实实际数值保存进 assessment 审计**
- [ ] **Step 6: 扫描生产代码确保没有域名或 `/archives/` 特判**
- [ ] **Step 7: 运行分类回归**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_page_classification.py services/api/tests/test_web_v8_primary_subject_capture.py services/api/tests/test_web_page_rule_capture_regressions.py -q`

- [ ] **Step 8: 提交本任务**

```text
[AI编程] fix(web): 修复通用图文详情结构识别

模块：Web 页面分类；数据库迁移：否；前端菜单/路由：不涉及；风险：详情与视频详情边界。
```

### Task 7: Task/Run 成年声明冻结一致性

**Files:**
- Modify: `services/api/app/application/task_submission.py`
- Modify: `services/api/app/infrastructure/db/task_repository.py`
- Test: `services/api/tests/test_v2_task_entrypoints.py`
- Test: `services/api/tests/test_task_submission.py`

**Interfaces:**
- Produces: 所有 Web V2 `qc_task.task_config._task_target` 与 Run 输入完全一致。

- [ ] **Step 1: 写 exploration_only 任务主表也保存 `_task_target` 的失败测试**
- [ ] **Step 2: 写 Task/Run 成年声明值完全一致的持久化测试**
- [ ] **Step 3: 移除 `_task_target` 写入条件中的 full_inspection 限制**
- [ ] **Step 4: 验证旧任务读取和非 Web 平台不受影响**
- [ ] **Step 5: 运行任务提交测试**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_v2_task_entrypoints.py services/api/tests/test_task_submission.py -q`

- [ ] **Step 6: 提交本任务**

```text
[AI编程] fix(web): 对齐探索任务与运行目标快照

模块：Web 任务提交；数据库迁移：否；前端菜单/路由：不涉及；风险：既有任务不回填。
```

### Task 8: 报告、查询和队列状态

**Files:**
- Modify: `services/api/app/infrastructure/db/web_report_page_snapshot.py`
- Modify: `services/api/app/application/web_report_explanation.py`
- Modify: `services/api/app/application/report_insight.py`
- Modify: `services/api/app/application/workstation_tasks.py`
- Modify: 与现有任务详情直接对应且未被用户删除的前端组件
- Test: `services/api/tests/test_web_report_explanation.py`
- Test: `services/api/tests/test_report_insight.py`
- Test: `services/api/tests/test_workstation_task_service.py`

**Interfaces:**
- Produces: 长图、广告和业务页独立明细，以及 queued/pending/running/失败分类。

- [ ] **Step 1: 写报告按 capture purpose 区分长图和广告的失败测试**
- [ ] **Step 2: 写 `ad_page` 不进入业务覆盖率、但进入证据详情的测试**
- [ ] **Step 3: 写 queued/pending 不显示为 configuration_error 的测试**
- [ ] **Step 4: 实现后端投影和不冲突的前端展示**
- [ ] **Step 5: 运行报告与工作台测试；若前端发生修改，运行对应 TypeScript 检查**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_report_explanation.py services/api/tests/test_report_insight.py services/api/tests/test_workstation_task_service.py -q`

- [ ] **Step 6: 提交本任务**

```text
[AI编程] feat(web): 展示动态探索证据与队列终态

模块：Web 报告与工作台；数据库迁移：否；前端菜单/路由：不新增；风险：旧报告序列化兼容。
```

### Task 9: 整体审核、回归与三站实况验证

**Files:**
- Create: `docs/reports/2026-09-14-web-v2-dynamic-exploration-code-review.md`
- Create: `docs/reports/2026-09-14-web-v2-three-site-live-validation.md`
- Create: `outputs/web-v2-dynamic-exploration-20260914/` 下三站详情和最终报告
- Modify: `docs/专项能力与高频回归规则.md`

**Interfaces:**
- Produces: 代码审核报告、三站探索详情、URL/页面类型清单、长图/广告证据统计和最终报告。

- [ ] **Step 1: 运行完整精确 Web 回归**

Run: `D:\project\Python\alioth\.venv\Scripts\python.exe -m pytest services/api/tests/test_web_*.py services/api/tests/test_v2_task_entrypoints.py services/api/tests/test_task_submission.py -q`

- [ ] **Step 2: 运行 App/H5 受影响边界回归和前端检查**

- [ ] **Step 3: 按设计合同执行代码审核，先修复所有 P0/P1 问题**

审核至少覆盖白名单残留执行依赖、跨域安全、长图幂等、广告隔离、页型闭集、历史恢复、Task/Run 一致性和用户工作区改动隔离。

- [ ] **Step 4: 启动或复用本地 Docker 服务并确认迁移、API、Worker 健康**

- [ ] **Step 5: 顺序提交并完成以下三个 exploration_only 任务**

```text
https://chigua.com/
https://heiliao.com/
https://hl365.com/
```

- [ ] **Step 6: 导出每站探索详情**

每个站点至少打印：请求 URL、最终 URL、页面类型、置信度、分类理由、来源 Capture、视口截图、全页长图、父子谱系、广告 `ad_page`、导航深度、探索路径、失败原因和覆盖状态。

- [ ] **Step 7: 导出三站最终报告并核对数据库证据原件存在**

- [ ] **Step 8: 更新专项回归规则并提交审核/验证产物**

```text
[AI编程] test(web): 完成三站动态探索与证据验收

模块：Web 实况验证与规则文档；数据库迁移：记录实际迁移；前端菜单/路由：记录实际影响；风险：外站内容和网络状态具有时效性。
```

## Completion Gate

- [ ] 新 Run 不读取站点白名单作为执行授权。
- [ ] 三个目标站点均无需新增域名/菜单配置即可运行。
- [ ] 每个稳定业务页面拥有主截图和最多一张真正的全页长图。
- [ ] 广告打开成功后保存为 `ad_page`，不再进入 evidence_insufficient/unknown。
- [ ] 通用文章、图文、图集和漫画详情稳定判为 `content_detail`，没有站点特判。
- [ ] Task 与 Run 的成年声明快照一致。
- [ ] 报告能够区分长图、广告、生命周期和 unknown。
- [ ] 代码审核无未解决 P0/P1。
- [ ] 精确回归、边界回归和三站实况证据均已保存。
