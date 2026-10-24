# Web V2 探索分类、去重与报告可见性实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Web V2 每个实际采集的业务逻辑页面都具备可审计的四类页面判定和质检终态，同一规范 URL 在工作台只显示一个逻辑页面，同时补齐六站 URL 分类提示、成年门禁、非广告主体截图、Web 报告预览以及平台专属运营组件边界。

**Architecture:** 第一交付批次先复用现有持久化事实开放 `/reports?task=<任务ID>` 预览，不等待页型与去重重构；预览明确不可导出。随后保留“安全导航白名单”和“页面类型判定”两个领域边界：白名单只授权同源只读导航，分类提示只是输入证据，最终类型由 URL 提示、渲染后 DOM 结构、主体截图和模型闭集判定共同决定。以规范 URL 建立逻辑页面身份，把基线、清障、滚动补证等 Capture 聚合为版本证据；第二报告批次再让预览和正式报告消费逻辑页面的已选主版本，不再按每张截图重复统计。

**Tech Stack:** Python 3.12、FastAPI、PostgreSQL、Microsoft Agent Framework、Playwright、Vue 3、Vite、TypeScript、pytest、Vitest。

**Spec:** `docs/superpowers/plans/2026-09-04-web-v2-exploration-quality-remediation.md#需求基线`

## Global Constraints

- 目标分支为 `codex/web-v2-exploration-1.1`，只改变 Web V2；H5/App 的页型、探索、Publication、报告和运营处置合同保持不变。
- Web 正式业务页型只允许 `video_list`、`video_detail`、`content_list`、`content_detail`；`home` 是页面角色，`mixed` 是辅助特征，`other` 是采集资格结果，均不是正式业务页型。
- URL/菜单提示不得越权覆盖 DOM 结构事实；提示与主体结构冲突时必须降低置信度或进入人工复核。
- “每个采集页面都有类型和质检”指已形成 ready baseline 的业务逻辑页面；菜单限额跳过、跨域阻断、功能入口排除项不伪造成已采集页面。
- 2026-09-05 补充需求将新任务 `WEB_MENU_COLLECTION_LIMIT` 扩大为 10；历史已冻结的 `menu_limit=5` 预算继续按原槽位幂等读取和续跑，禁止原地扩容。跳过条目继续以明确终态记账，但不进入页面时间线。
- 同一规范 URL 只能形成一个逻辑页面卡片；页面重新截图、弹窗清理、广告规避滚动和内容补证只形成版本或证据附件。
- 首屏真实截图必须保留；滚动到非广告主体后的截图只能作为补充证据，不能隐藏广告事实。
- 年龄门禁只允许点击明确的“已成年/年满 18/进入/继续”确认候选；验证码、滑块和其他人机验证一律不点击。
- Web 正式报告只有通过 Completion Gate 并绑定快照后才可导出；运行中或未过门禁时只提供带水印、不可导出的预览报告。
- 页面内容、DOM 和截图中的文字一律视为不可信数据，不得成为系统指令。
- 优先复用现有 JSON/metadata、Capture 父子关系和幂等键；菜单上限补充需求只新增一条“修改新预算默认值、不改历史行”的数据库迁移。
- 所有注释使用中文 UTF-8；文档放在 `docs`；生产行为必须先写失败测试，再写最小实现。

---

## 需求基线

### 1. 页面类型与质检完整性

- 当前低置信度、模型返回不受支持类型或 DOM 结构门不通过时，`WebPageClassifier` 会返回 `page_type=None`。
- Probe、清障、ready baseline、详情和补证 Capture 容易在读取模型中被当成多张页面展示，用户看到“采集很多、很多未定义”。
- 新合同必须保留 `classification_status`、`proposed_page_type`、`final_page_type`、置信度、证据和复核原因；不得只显示“未定义”。
- 最终收口必须检查每个已采集业务逻辑页面是否拥有页面分类终态和质检终态。

### 2. 页型映射移除

- 当前 Web 分类目录输出 `home/web_video_list/web_video_detail/web_content_list/web_content_detail`，再由 `web_page_type_bridge.py` 映射到正式词表。
- 新写入链路直接使用四种正式类型，不再产生新的 `web_*` 页型或 `web_page_type_normalization.v1` 元数据。
- 历史报告仍可读取旧 `web_*` 数据，但兼容逻辑只能存在于查询适配器，不能回流到新运行时。

### 3. URL 白名单与六站分类提示

- 生产菜单目录已有六站、157 个可观察菜单及真实 `href`；现有 `decision` 只表达是否允许导航。
- 已存在且能通过 `entry_id + canonical URL` 对账的用户分类可增加 `classification_hint`。
- 用户表中未出现在当前 Catalog 的菜单名称不得臆造 URL，必须先由 ready 首页 DOM 提取真实 `href` 并通过同源安全审核。
- `mixed` 转换为 `mixed_content=true` 并选择主要形态；APP、联系方式、外链和纯功能入口标记为排除或保持跨域阻断。

### 4. HTML 导航和结构辅助分类

- 首页完成年龄门禁与普通阻断清理后，从渲染 DOM 的 `nav/header/menu` 和重复锚点结构采集导航链接。
- 页面分类采用两个维度：列表/详情轴和视频/内容轴；重复卡片、主详情链接、正文主体、视频播放器、媒体卡片和广告排除共同提供结构事实。

### 5. 成年门禁

- 系统提示明确任务操作者为成年人；检测到真正阻断整站的年龄门禁时，应优先从密封候选中选择明确的成年确认控件。
- 注册页成年勾选、正文中的 18+ 文本、验证码或含糊按钮不得当作入口年龄确认。

### 6. 广告较多页面的截图

- 保留 `web_page_ready_baseline` 首屏真实状态。
- 再按主体 DOM 目标滚动，最多两次，生成 `web_primary_content_evidence`；列表页对准有效重复卡片，详情页对准正文或主播放器。
- 固定广告无法绕过、新弹窗出现、URL 改变或人机验证出现时停止并记录明确诊断。

### 7. Web 报告页面

- 当前任务详情只有 `report_available=true` 才显示 Web 正式报告入口；报告服务在没有门禁绑定快照时拒绝 Web V2 fallback，因此运行中没有可见报告。
- 新合同提供 `unavailable/preview/final` 三态；preview 从持久化探索、规则和证据事实组装，不冒充正式报告。

### 8. Web 详情出现 H5/App Publication 卡片

- 截图对应 `DeliveryIncidentPanel`。当前父页面只检查 `systemOperations` 权限，没有检查任务平台，所以 Web 管理员也会看到“仅展示 H5/App”的空运营组件。
- Web 任务必须隐藏该组件并停止请求 H5/App 投递事故接口；未来若 Web 需要运营处置，应建立 Web 专属投递视图。

---

## 文件结构与职责

### 新建

- `services/api/app/application/web_page_type_assessment.py`：直接持久化四类正式判定，替代页型映射桥。
- `services/api/app/application/web_page_classification_hints.py`：组合站点、URL、菜单和 DOM 提示，输出非权威候选证据。
- `services/api/app/application/web_logical_page.py`：规范 URL、逻辑页面身份、版本选择和证据归属。
- `services/api/app/application/web_primary_content_capture.py`：选择非广告主体区域并生成有界滚动补证计划。
- `services/api/migrations/versions/20260905_0090_web_menu_limit_ten.py`：把新预算默认菜单上限改为 10，不更新历史五菜单预算。

### 删除或退役

- `services/api/app/application/web_page_type_bridge.py`：新运行时不再使用映射桥；历史读取兼容移入查询适配器。
- `services/api/app/domain/exploration/web5_coverage_scope.py`：由四类正式页型常量替代，首页覆盖改用角色状态表达。

### 主要修改

- `services/api/app/domain/page/web_page_type_catalog.json`
- `services/api/app/domain/page/web_site_menu_catalog.json`
- `services/api/app/domain/page/web_site_menu_registry.py`
- `services/api/app/domain/exploration/web_exploration.py`
- `services/api/app/domain/exploration/web_coverage_snapshot.py`
- `services/api/app/application/web_page_classification.py`
- `services/api/app/application/web_desktop_navigation.py`
- `services/api/app/application/web_menu_exploration.py`
- `services/api/app/application/web_list_detail_navigation.py`
- `services/api/app/application/web_runtime_composition.py`
- `services/api/app/application/web_inspection_reconciliation.py`
- `services/api/app/application/web_page_inspection_notification.py`
- `services/api/app/agents/explorer/web_assessment_agent.py`
- `services/api/app/infrastructure/browser/page_capture.py`
- `services/api/app/infrastructure/browser/capture_runtime.py`
- `services/api/app/infrastructure/browser/web_desktop_capture_policy.py`
- `services/api/app/infrastructure/db/web_capture_evidence_repository.py`
- `services/api/app/infrastructure/db/web_coverage_repository.py`
- `services/api/app/infrastructure/db/web_coverage_snapshot_repository.py`
- `services/api/app/infrastructure/db/task_insight_repository.py`
- `services/api/app/application/task_insight.py`
- `services/api/app/application/workstation_reports.py`
- `services/api/app/infrastructure/db/workstation_query_repository.py`
- `app/workstation/src/modules/tasks/pages/TaskDetailView.vue`
- `app/workstation/src/modules/tasks/components/DeliveryIncidentPanel.vue`
- `app/workstation/src/modules/reports/pages/ReportCenterView.vue`
- `app/workstation/src/types/task.ts`
- `docs/data/web_v2_six_site_menu_allowlist.json`
- `docs/专项能力与高频回归规则.md`

---

### Task 0: 先开放 Web 报告预览页面

**Files:**
- Modify: `services/api/app/application/workstation_reports.py`
- Modify: `services/api/app/infrastructure/db/workstation_query_repository.py`
- Modify: `app/workstation/src/types/task.ts`
- Modify: `app/workstation/src/modules/tasks/pages/TaskDetailView.vue`
- Modify: `app/workstation/src/modules/reports/pages/ReportCenterView.vue`
- Test: `services/api/tests/test_workstation_reports_api.py`
- Test: `app/workstation/src/modules/tasks/pages/TaskDetailView.test.ts`

**Interfaces:**

```typescript
type WebReportAvailability = 'unavailable' | 'preview' | 'final'
```

- [ ] **Step 1: 写 Web 报告入口失败测试**

Web V2 已有页面 Capture、规则结果或探索摘要但尚未通过 Completion Gate 时，任务详情必须显示“查看 Web 报告”，并跳转 `/reports?task=<task_id>`；没有任何持久化事实时保持 `unavailable`。

- [ ] **Step 2: 写预览报告失败测试**

`WorkstationReportService.get_report()` 在没有最终快照但已有 Web 持久化事实时返回 `report_availability="preview"`、`is_final=false`、`export_allowed=false` 和未完成原因，不再抛出“正式报告尚未通过门禁”。

- [ ] **Step 3: 写最终报告非回归测试**

Completion Gate 通过且 report binding/payload 同版本时继续返回不可变最终快照，并标记 `report_availability="final"`、`is_final=true`、`export_allowed=true`。

- [ ] **Step 4: 实现最小三态查询与页面展示**

预览只读取当前已有的页面时间线、探索摘要、规则结果和截图引用，不改变当前分类结果，不创建 Completion Gate 或报告绑定。ReportCenter 显示“预览报告”水印和未完成说明，隐藏导出按钮。

- [ ] **Step 5: 运行后端与前端测试**

Run: `docker exec alioth-api python -m pytest services/api/tests/test_workstation_reports_api.py -q -k "web and (preview or final or availability)"`

Run: `npm --prefix app/workstation run test -- TaskDetailView.test.ts`

Expected: 全部通过，已有 Web 持久化事实的任务可以进入报告页。

- [ ] **Step 6: 提交首批可见结果**

Commit: `[AI编程] feat(web): 优先开放Web预览报告；无数据库迁移；前端报告入口已同步；当前预览仍按既有Capture结构展示`

### 第一交付批次验收

1. 任务详情出现“查看 Web 报告”。
2. `/reports?task=<任务ID>` 能显示当前已有页面、规则、截图和未完成原因。
3. 预览明确标识非正式且无法导出。
4. 已有正式报告的行为不变。

### Task 1: 建立四类正式页型合同并移除新写入映射

**Files:**
- Create: `services/api/app/application/web_page_type_assessment.py`
- Modify: `services/api/app/domain/page/web_page_type_catalog.json`
- Modify: `services/api/app/domain/page/web_page_catalog.py`
- Modify: `services/api/app/domain/exploration/web_exploration.py`
- Modify: `services/api/app/domain/exploration/web_coverage_snapshot.py`
- Modify: `services/api/app/application/web_page_classification.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/infrastructure/db/web_capture_evidence_repository.py`
- Delete after callers migrate: `services/api/app/application/web_page_type_bridge.py`
- Delete after callers migrate: `services/api/app/domain/exploration/web5_coverage_scope.py`
- Test: `services/api/tests/test_web_page_type_catalog.py`
- Test: `services/api/tests/test_web_page_classification.py`
- Replace: `services/api/tests/test_web_page_type_bridge.py` with `services/api/tests/test_web_page_type_assessment.py`

**Interfaces:**

```python
WEB_PAGE_TYPES = (
    "video_list",
    "video_detail",
    "content_list",
    "content_detail",
)

class WebPageTypeAssessmentRecorder:
    async def persist(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        capture: WebReadyPageCapture,
        assessment: WebPageAssessment,
    ) -> str | None: ...
```

- [ ] **Step 1: 写四类 Catalog 失败测试**

```python
def test_web_catalog_uses_only_formal_business_page_types() -> None:
    assert load_web_page_type_catalog().codes == WEB_PAGE_TYPES
    assert "home" not in WEB_PAGE_TYPES
    assert all(not item.startswith("web_") for item in WEB_PAGE_TYPES)
```

- [ ] **Step 2: 写首页角色与页型分离测试**

首页 Capture 保持 `page_role="home"`，但允许依据主体结构得到 `content_list` 或 `video_list`；详情角色不得得到首页伪类型。

- [ ] **Step 3: 写直接持久化测试**

断言新 metadata 使用 `web_page_type_assessment.v2`，直接保存正式 `page_type`；新写入不得包含 `raw_page_type=web_*` 或 `web_page_type_normalization`。

- [ ] **Step 4: 运行 RED**

Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_page_classification.py services/api/tests/test_web_page_type_assessment.py -q`

Expected: FAIL，因为当前 Catalog 仍是五类且 Runtime 依赖映射桥。

- [ ] **Step 5: 实现四类 Catalog 和直接 Assessment Recorder**

将首页导航能力保留在 `page_role`，四类业务类型成为分类、覆盖和规则匹配的唯一枚举。Recorder 复用现有事务与 Assessment 表，不新增数据库表。

- [ ] **Step 6: 迁移调用方并删除映射桥**

更新 Runtime、Coverage、Reconciliation、Repository 和 API Schema 中的 `web_*` 判断；新代码不得导入 `WEB_PAGE_TYPE_ALIASES`。

- [ ] **Step 7: 运行 GREEN**

Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_page_classification.py services/api/tests/test_web_page_type_assessment.py services/api/tests/test_web_coverage_snapshot.py services/api/tests/test_web_runtime_composition.py -q`

Expected: 全部通过。

- [ ] **Step 8: 提交**

Commit: `[AI编程] refactor(web): 统一四类正式页型；无数据库迁移；无需前端菜单路由；历史web前缀读取在后续任务覆盖`

### Task 2: 为六站 URL 白名单增加非权威分类提示

**Files:**
- Create: `services/api/app/application/web_page_classification_hints.py`
- Modify: `services/api/app/domain/page/web_site_menu_catalog.json`
- Modify: `services/api/app/domain/page/web_site_menu_registry.py`
- Modify: `docs/data/web_v2_six_site_menu_allowlist.json`
- Test: `services/api/tests/test_web_site_menu_registry.py`
- Test: `services/api/tests/test_web_six_site_shadow_fixtures.py`

**Interfaces:**

```python
@dataclass(frozen=True, kw_only=True)
class WebPageClassificationHint:
    expected_page_type: str
    confidence: float
    source: str
    mixed_content: bool = False

@dataclass(frozen=True, kw_only=True)
class WebCollectionEligibility:
    eligible: bool
    reason: str | None = None
```

- [ ] **Step 1: 写 Schema v2 失败测试**

断言 `classification_hint.expected_page_type` 只接受四类，`confidence` 位于 `[0,1]`，提示字段不会改变 `WebMenuDecision`。

- [ ] **Step 2: 写六站精确 URL 对账测试**

对已存在的 `entry_id + canonical_url` 补充用户给出的分类；同名菜单在不同路径下允许不同提示。`91porna.video.gossip` 预期 `video_list`，独立黑料栏目预期 `content_list`。

- [ ] **Step 3: 写混合与其他边界测试**

“混合”条目必须落为四类之一并带 `mixed_content=true`；首页使用 `home_baseline` 加站点主导列表提示；跨域 APP 保持 `blocked_cross_origin`；功能入口写 `eligible=false`，不得进入分类分母。

- [ ] **Step 4: 写缺失菜单拒绝测试**

用户表中当前 Catalog 不存在的 `91jav1`、`huangguoai`、`avhub360` 菜单名称不得新增无证据 `href`；只允许在 ready DOM 发现真实同源链接后进入待审核目录。

- [ ] **Step 5: 运行 RED**

Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_six_site_shadow_fixtures.py -q -k "classification_hint or eligibility or canonical"`

Expected: FAIL，因为 v1 条目尚不解析类型提示。

- [ ] **Step 6: 实现并同步两份 Catalog**

生产文件是 `services/api/app/domain/page/web_site_menu_catalog.json`；`docs/data` 仅作审计副本。增加语义一致性测试，避免两份目录内容漂移。

- [ ] **Step 7: 运行 GREEN**

Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_six_site_shadow_fixtures.py -q`

Expected: 全部通过。

- [ ] **Step 8: 提交**

Commit: `[AI编程] feat(web): 增加六站URL分类提示；无数据库迁移；无需前端菜单路由；未发现真实href的菜单暂不纳入`

### Task 3: 用 ready DOM 导航和两轴结构判定提高准确率

**Files:**
- Modify: `services/api/app/application/web_desktop_navigation.py`
- Modify: `services/api/app/application/web_page_classification.py`
- Modify: `services/api/app/agents/explorer/web_assessment_agent.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Test: `services/api/tests/test_web_page_classification.py`
- Test: `services/api/tests/test_web_exploration_assessment_agent.py`
- Test: `services/api/tests/test_web_menu_exploration.py`

**Interfaces:**

```python
@dataclass(frozen=True, kw_only=True)
class WebPageStructureFacts:
    repeated_primary_card_count: int
    primary_detail_link_count: int
    article_body_count: int
    paragraph_text_length: int
    video_element_count: int
    player_root_count: int
    media_card_count: int
    advertisement_element_count: int
```

- [ ] **Step 1: 写 ready 首页导航提取测试**

只有完成年龄门禁和普通阻断后的 DOM 才可生成导航候选；隐藏、广告、页脚、分页、下载和跨域链接不能提升为同源采集目标。

- [ ] **Step 2: 写列表/详情轴测试**

重复可信卡片和至少两个主详情链接判为列表证据；单一正文主体或单一播放器判为详情证据；导航链接数量不能替代业务卡片数量。

- [ ] **Step 3: 写视频/内容轴测试**

播放器、媒体资源、海报和视频卡片支持视频判定；`article/main`、标题和正文段落支持内容判定；页面仅含视频附件时不得自动把文章详情改成视频详情。

- [ ] **Step 4: 写提示冲突测试**

URL 提示为 `video_list`、DOM 明确为文章正文时返回 `needs_review` 或采纳高置信 DOM 的 `content_detail`，审计证据必须包含 `URL_HINT_DOM_CONFLICT`。

- [ ] **Step 5: 扩展 Agent 输入**

将结构事实、规范 URL 提示和菜单路径提示作为结构化字段传入；系统提示继续声明 URL 和菜单名称不能单独定案。

- [ ] **Step 6: 运行定向测试**

Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_page_classification.py services/api/tests/test_web_exploration_assessment_agent.py services/api/tests/test_web_menu_exploration.py -q`

Expected: 全部通过。

- [ ] **Step 7: 提交**

Commit: `[AI编程] feat(web): 增强DOM与URL辅助分类；无数据库迁移；无需前端菜单路由；复杂Canvas站点保留人工复核`

### Task 4: 建立逻辑页面身份并消除重复 URL 时间线

**Files:**
- Create: `services/api/app/application/web_logical_page.py`
- Modify: `services/api/app/domain/page/web_site_menu_registry.py`
- Modify: `services/api/app/application/web_menu_exploration.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/application/task_insight.py`
- Modify: `services/api/app/infrastructure/db/task_insight_repository.py`
- Modify: `app/workstation/src/modules/tasks/pages/TaskDetailView.vue`
- Test: `services/api/tests/test_web_site_menu_registry.py`
- Test: `services/api/tests/test_web_menu_exploration.py`
- Test: `services/api/tests/test_task_insight_service.py`
- Test: `app/workstation/src/modules/tasks/pages/TaskDetailView.test.ts`

**Interfaces:**

```python
def canonical_web_logical_page_url(url: str) -> str: ...

@dataclass(frozen=True, kw_only=True)
class WebLogicalPage:
    logical_page_id: str
    canonical_url: str
    menu_entry_ids: tuple[str, ...]
    capture_ids: tuple[str, ...]
    primary_capture_id: str
```

- [ ] **Step 1: 写规范 URL 测试**

协议和主机小写、默认端口移除、空路径归一为 `/`、fragment 移除；同 host 的 HTTP→HTTPS 升级可对账；业务 query 保留，已批准的追踪参数不参与身份。

- [ ] **Step 2: 写菜单别名合并测试**

同一规范 URL 的多个 `menu_path/entry_id` 保留为别名，但只进入一次待访问队列、只消耗一次页面预算。

- [ ] **Step 3: 写 Capture 版本聚合测试**

ready baseline、弹窗关闭后重采、滚动补证和同 URL 重访全部归入同一 `logical_page_id`；URL 改变才建立新逻辑页面。

- [ ] **Step 4: 写工作台时间线测试**

同一 URL 的多个 Capture 只渲染一个卡片，卡片显示证据数量、当前主版本和历史版本；`node_seq` 不再作为页面唯一键。

- [ ] **Step 5: 实现 Runtime 去重与读模型聚合**

探索写入 metadata 中的 `logical_page_id` 和 `capture_purpose`；旧数据由读取层按规范 URL 回退聚合，不新增数据库列。

- [ ] **Step 6: 运行后端和前端测试**

Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_task_insight_service.py -q`

Run: `npm --prefix app/workstation run test -- TaskDetailView.test.ts`

Expected: 全部通过。

- [ ] **Step 7: 提交**

Commit: `[AI编程] fix(web): 按逻辑URL合并页面时间线；无数据库迁移；前端任务详情已同步；历史异常query仍需实况观察`

### Task 5: 强制每个采集逻辑页面完成分类与质检闭环

**Files:**
- Modify: `services/api/app/application/web_page_classification.py`
- Modify: `services/api/app/application/web_page_inspection_notification.py`
- Modify: `services/api/app/application/web_inspection_reconciliation.py`
- Modify: `services/api/app/domain/exploration/web_coverage_snapshot.py`
- Modify: `services/api/app/infrastructure/db/web_capture_evidence_repository.py`
- Modify: `services/api/app/infrastructure/db/web_coverage_snapshot_repository.py`
- Test: `services/api/tests/test_web_page_classification.py`
- Test: `services/api/tests/test_web_page_inspection_notification.py`
- Test: `services/api/tests/test_web_inspection_reconciliation.py`
- Test: `services/api/tests/test_web_formal_terminal_ledger_pg.py`

**Interfaces:**

```python
ClassificationStatus = Literal["classified", "needs_review", "excluded"]
InspectionStatus = Literal["completed", "evidence_insufficient", "needs_review", "failed"]
```

- [ ] **Step 1: 写无裸空类型测试**

低置信度和结构不足时仍保存 `proposed_page_type`、原因和证据，工作台显示“候选类型 · 待复核”，不得只返回 `page_type=None`。

- [ ] **Step 2: 写有界重判测试**

分类失败最多使用同一 ready 证据重判两次；补到主体截图后允许再判一次；超过预算进入 `needs_review`，不得无限调用模型或重新导航。

- [ ] **Step 3: 写逻辑版本幂等通知测试**

同一 `logical_page_id + content_fingerprint` 只产生一组 Publication/Work；补充截图附着到既有证据包。内容指纹变化时形成新版本，但时间线仍是一张逻辑页面卡片。

- [ ] **Step 4: 写 Completion Gate 测试**

每个已采集且 `eligible=true` 的逻辑页面必须有分类终态和质检终态；存在未分类、未投递或未终态 Work 时 Gate 不通过，并列出逻辑页面 ID。

- [ ] **Step 5: 实现分类、通知和收口检查**

Coverage Snapshot 按四类正式页型和逻辑页面版本冻结；菜单限额跳过、跨域阻断和排除项只进入菜单账本，不进入页面质检分母。

- [ ] **Step 6: 运行测试**

Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_page_classification.py services/api/tests/test_web_page_inspection_notification.py services/api/tests/test_web_inspection_reconciliation.py services/api/tests/test_web_formal_terminal_ledger_pg.py -q`

Expected: 全部通过。

- [ ] **Step 7: 提交**

Commit: `[AI编程] fix(web): 强制逻辑页面分类质检收口；无数据库迁移；无需前端菜单路由；人工复核SLA不在本次范围`

### Task 6: 强化成年确认提示并保持安全候选闭集

**Files:**
- Modify: `services/api/app/agents/explorer/web_assessment_agent.py`
- Modify: `services/api/app/application/web_age_gate.py`
- Test: `services/api/tests/test_web_exploration_assessment_agent.py`
- Test: `services/api/tests/test_web_age_gate.py`
- Test: `services/api/tests/test_web_page_readiness.py`

- [ ] **Step 1: 写成年引导测试**

Agent 指令明确：“任务操作者为成年人；真正的入口年龄门禁存在时，从候选中选择语义明确的已成年、年满 18、进入或继续按钮。”

- [ ] **Step 2: 写禁止边界测试**

注册同意、正文 18+、验证码、滑块、支付、下载和含糊图片按钮均不得返回确认候选。

- [ ] **Step 3: 增加结构化约束**

在 `adult_age_gate` payload 中增加 `operator_is_adult=true` 和 `prefer_explicit_age_confirmation_candidate=true`，但候选仍必须来自密封 DOM 闭集并经过执行前复验。

- [ ] **Step 4: 运行测试**

Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_exploration_assessment_agent.py services/api/tests/test_web_age_gate.py services/api/tests/test_web_page_readiness.py -q`

Expected: 全部通过。

- [ ] **Step 5: 提交**

Commit: `[AI编程] fix(web): 强化成年入口确认引导；无数据库迁移；无需前端菜单路由；站点文案变体需六站实况验证`

### Task 7: 滚动到非广告主体区域生成补充截图

**Files:**
- Create: `services/api/app/application/web_primary_content_capture.py`
- Modify: `services/api/app/application/web_agent_page_capture.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Modify: `services/api/app/infrastructure/browser/capture_runtime.py`
- Modify: `services/api/app/infrastructure/browser/web_desktop_capture_policy.py`
- Modify: `services/api/app/infrastructure/db/web_capture_evidence_repository.py`
- Test: `services/api/tests/test_web_capture_runtime.py`
- Test: `services/api/tests/test_web_agent_page_capture.py`
- Test: `services/api/tests/test_web_page_classification.py`

**Interfaces:**

```python
@dataclass(frozen=True, kw_only=True)
class WebPrimaryContentTarget:
    candidate_id: str
    dom_fingerprint: str
    expected_page_type: str
    viewport_ad_coverage: float
    target_score: float
```

- [ ] **Step 1: 写主体目标选择测试**

列表类型选择至少两个非广告业务卡片的重复容器；内容详情选择正文；视频详情选择主播放器；导航、页脚、分页、推广 iframe 和广告播放器被排除。

- [ ] **Step 2: 写目标感知滚动测试**

使用密封候选执行 `scrollIntoView({block:"center"})` 或受限 `scrollTo`，替代无目标的连续 `0.85` 视口滚动；动作后重新观察 URL、目标指纹和阻断状态。

- [ ] **Step 3: 写双层证据测试**

始终保留 `web_page_ready_baseline`，成功后新增 `web_primary_content_evidence`；两张截图共享 `logical_page_id`，补图带 `parent_capture_id`、滚动偏移和广告覆盖变化。

- [ ] **Step 4: 写有界失败测试**

最多两次滚动；固定广告仍遮挡时记录 `WEB_PRIMARY_CONTENT_VIEWPORT_BLOCKED`；URL 改变、新弹窗或人机验证出现时停止，不能生成伪干净证据。

- [ ] **Step 5: 实现并接入分类证据**

分类器可读取主体补图和完整 DOM，但广告质检与报告仍保留首屏基线。补图不创建新时间线页面或新 Publication。

- [ ] **Step 6: 运行测试**

Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_capture_runtime.py services/api/tests/test_web_agent_page_capture.py services/api/tests/test_web_page_classification.py -q -k "primary_content or advertisement or scroll"`

Expected: 全部通过。

- [ ] **Step 7: 提交**

Commit: `[AI编程] feat(web): 增加非广告主体补充截图；无数据库迁移；报告证据画廊需下一任务同步；固定悬浮广告保留复核`

### Task 8: 将报告升级为逻辑页面正式视图

**Files:**
- Modify: `services/api/app/application/workstation_reports.py`
- Modify: `services/api/app/infrastructure/db/workstation_query_repository.py`
- Modify: `services/api/app/application/task_insight.py`
- Modify: `app/workstation/src/types/task.ts`
- Modify: `app/workstation/src/modules/tasks/pages/TaskDetailView.vue`
- Modify: `app/workstation/src/modules/reports/pages/ReportCenterView.vue`
- Test: `services/api/tests/test_workstation_reports_api.py`
- Test: `services/api/tests/test_web_v2_formal_report_e2e.py`
- Test: `app/workstation/src/modules/tasks/pages/TaskDetailView.test.ts`

**Interfaces:**

- Consumes: Task 0 的 `WebReportAvailability` 和预览页面、Task 4 的 `WebLogicalPage`、Task 5 的逻辑页面质检终态、Task 7 的双层截图证据。
- Produces: 预览与正式报告统一的逻辑页面视图，正式导出仍严格依赖 Completion Gate 和报告绑定。

- [ ] **Step 1: 写报告证据画廊测试**

报告按逻辑页面展示首屏基线、主体内容补图和当前质检结果；同一 URL 不重复计入页面总数。

- [ ] **Step 2: 写分类与质检终态测试**

逻辑页面展示最终四类类型；待复核页面展示候选类型、置信度和原因；排除菜单只出现在探索摘要，不进入报告页面计数。

- [ ] **Step 3: 写正式快照稳定性测试**

预览可以随当前 Run 更新；final 必须只读取同一 Gate/epoch/version 的不可变快照。补充截图不得重新创建报告绑定或改变已封存正式报告。

- [ ] **Step 4: 升级同一 `/reports?task=` 页面**

不新建第二套 Web 报告路由；ReportCenter 将 Task 0 的既有 Capture 预览升级为逻辑页面聚合，并根据平台和可用状态切换预览/正式展示。

- [ ] **Step 5: 运行测试**

Run: `docker exec alioth-api python -m pytest services/api/tests/test_workstation_reports_api.py services/api/tests/test_web_v2_formal_report_e2e.py -q`

Run: `npm --prefix app/workstation run test -- TaskDetailView.test.ts`

Expected: 全部通过。

- [ ] **Step 6: 提交**

Commit: `[AI编程] feat(web): 升级逻辑页面正式报告；无数据库迁移；前端报告视图已同步；正式快照仍受Gate绑定保护`

### Task 9: Web 任务隐藏 H5/App 投递事故组件

**Files:**
- Modify: `app/workstation/src/modules/tasks/pages/TaskDetailView.vue`
- Modify: `app/workstation/src/modules/tasks/components/DeliveryIncidentPanel.vue`
- Test: `app/workstation/src/modules/tasks/pages/TaskDetailView.test.ts`
- Test: `app/workstation/src/modules/tasks/components/DeliveryIncidentPanel.test.ts`

- [ ] **Step 1: 写平台边界失败测试**

Web 任务即使用户有 `systemOperations` 权限，也不渲染 `delivery-incident-panel`，且不调用 `listDeliveryIncidents`；H5/App 行为保持不变。

- [ ] **Step 2: 收紧父组件显示条件**

```typescript
const canOperateDeliveryIncidents = computed(() =>
  ['h5', 'app'].includes(task.value?.target_platform ?? '') &&
  (authStore.user.value?.permissions.includes('systemOperations') ?? false),
)
```

- [ ] **Step 3: 增加组件级防御**

向组件传入平台并只接受 `h5 | app`，防止未来其他父页面误用。Web 若需要处置，另建 Web 专属组件和接口合同。

- [ ] **Step 4: 运行测试**

Run: `npm --prefix app/workstation run test -- TaskDetailView.test.ts DeliveryIncidentPanel.test.ts`

Expected: 全部通过。

- [ ] **Step 5: 提交**

Commit: `[AI编程] fix(workstation): 隔离Web与H5App投递组件；无数据库迁移；无需菜单路由；已覆盖平台回归`

### Task 10: 历史兼容、全链路回归和专项规则文档

**Files:**
- Modify: `services/api/app/infrastructure/db/task_insight_repository.py`
- Modify: `services/api/app/application/workstation_reports.py`
- Modify: `docs/专项能力与高频回归规则.md`
- Test: `services/api/tests/test_task_insight_repository.py`
- Test: `services/api/tests/test_web_evidence_bundle.py`
- Test: `services/api/tests/test_worker_web_v2_route.py`
- Test: `services/api/tests/test_web_v2_formal_report_e2e.py`

- [ ] **Step 1: 写历史读取兼容测试**

历史 `web_*` 和 `web_page_type_normalization.v1` 只在查询投影中转换为四类展示；任何新 Run 的数据库写入出现 `web_*` 时测试失败。

- [ ] **Step 2: 写全链路验收测试**

覆盖首页 ready、菜单提取、URL 提示、DOM 分类、唯一详情、主体补图、单逻辑页面 Publication、规则终态、Completion Gate、预览报告和正式报告。

- [ ] **Step 3: 运行 Web 全量回归**

Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_site_menu_registry.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_page_classification.py services/api/tests/test_web_page_inspection_notification.py services/api/tests/test_web_inspection_reconciliation.py services/api/tests/test_web_evidence_bundle.py services/api/tests/test_worker_web_v2_route.py services/api/tests/test_web_v2_formal_report_e2e.py services/api/tests/test_workstation_reports_api.py -q`

Expected: 全部通过。

- [ ] **Step 4: 运行 H5/App 非回归**

Run: `docker exec alioth-api python -m pytest services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_app_exploration_mode_parity.py services/api/tests/test_h5_inspection_publication.py -q`

Expected: 全部通过。

- [ ] **Step 5: 运行前端检查**

Run: `npm --prefix app/workstation run test`

Run: `npm --prefix app/workstation run type-check`

Expected: 全部通过。

- [ ] **Step 6: 更新高频回归规则**

记录四条强制规则：新 Web 页型不得带 `web_`；一个规范 URL 一个逻辑页面；补证截图不新增时间线/Publication；平台专属运营组件必须同时检查权限和平台。

- [ ] **Step 7: 提交**

Commit: `[AI编程] test(web): 完成分类去重报告全链路回归；无数据库迁移；前端报告与任务详情已同步；六站动态改版风险保留监控`

---

## 验收标准

1. 每个实际采集的业务逻辑页面显示四类正式类型之一，或显示“候选类型 + 待复核原因”，不再出现无解释的未定义。
2. 每个已采集业务逻辑页面都有 `classified/needs_review` 分类终态和 `completed/evidence_insufficient/needs_review/failed` 质检终态。
3. 同一规范 URL 在任务详情时间线只显示一次；菜单别名、重采和滚动补图显示在同一卡片中。
4. 新 Web Run 的模型输入、Capture、Assessment、Coverage、规则和报告均直接使用四类类型，不再写入 `web_*`。
5. 六站现有 URL 白名单的已确认条目带非权威分类提示；未确认真实 URL 的菜单没有被猜测加入。
6. 首页完成清障后能从渲染 DOM 读取导航；广告、页脚、分页、下载和跨域入口不会成为采集目标。
7. 真正年龄门禁可以选择明确成年确认按钮；验证码、人机验证和注册勾选不会被点击。
8. 广告较多页面同时保留首屏真实截图与非广告主体补图；补图不增加页面数量或重复质检。
9. Web 任务有持久化事实后可查看预览报告；只有 Gate 通过并绑定快照后才可查看和导出正式报告。
10. Web 任务详情不再显示“H5/App 主证据 Publication”运营处置面板，也不会发起对应接口请求。

## 调整规模与建议排期

| 子项目 | 规模 | 预计工作量 |
|---|---:|---:|
| 问题 1：分类准确率、逻辑页面去重、分类与质检收口 | 大 | 3～5 人日 |
| 问题 2：四类正式页型、移除新写入映射、历史只读兼容 | 中到大 | 2～3 人日 |
| 六站 URL 分类提示 | 中 | 0.5～1 人日 |
| 成年门禁提示增强 | 小 | 0.25～0.5 人日 |
| 非广告主体补充截图 | 中 | 1.5～3 人日 |
| 第一批：先开放 Web 报告预览 | 小到中 | 0.5～1 人日 |
| 第二批：逻辑页面正式报告升级 | 中 | 1～1.5 人日 |
| 隐藏 Web 页的 H5/App 运营组件 | 小 | 0.25 人日 |

存在文件和测试重叠，合并实施总量预计 **8～13 人日**。只做一天的最小修补可以减少空类型展示、按 URL 合并前端卡片并隐藏错误运营组件，但不能安全完成四类迁移、质检幂等、Completion Gate、历史兼容和报告闭环，因此不作为最终方案。

## 推荐实施顺序

1. **Task 0：第一批先开放 Web 报告预览，让 `/reports?task=<任务ID>` 立即可见。**
2. Task 1～5：完成问题 1、2 的领域合同、分类、去重和质检收口。
3. Task 6～7：在稳定的逻辑页面证据模型上加入成年确认和主体补图。
4. Task 8：把第一批报告升级为逻辑页面聚合和正式报告视图。
5. Task 9：隔离 Web 与 H5/App 投递运营组件；该任务也可与 Task 0 同批完成。
6. Task 10：执行全链路与 H5/App 非回归后再合并。
