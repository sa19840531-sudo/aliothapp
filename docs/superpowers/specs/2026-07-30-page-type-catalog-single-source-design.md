# 页面类型目录单一事实源设计

## 1. 背景

当前页面类型的 canonical code 以
`services/api/app/domain/page/page_type_catalog.json` 为主要来源，但运行时代码、
Agent 指令和 Skill 中仍存在多处重复清单或映射：

- `exploration_tools.py` 硬编码 12 个业务页面类型；
- `instructions.py` 与 `qc-workflow/SKILL.md` 重复声明相同覆盖范围；
- `page_exploration_summary.py` 单独维护生命周期类型；
- `page_inspection_memory.py` 单独维护瞬态类型；
- `page_exploration_profile.py`、`nav_alias.py` 与 `page_detection_tools.py`
  分别维护导航文案、别名和路由到页面类型的映射；
- 数据库导航别名表可以形成独立于 JSON 的运行时判断来源。

这些重复定义会导致新增或调整页面类型时需要修改多个文件，并可能出现页面识别、
探索收口、覆盖率统计、规则匹配与 Agent 提示词口径不一致。

## 2. 目标

本设计确立以下约束：

1. `page_type_catalog.json` 是页面类型定义的唯一事实源。
2. Python 代码、API、Agent 上下文、探索收口和覆盖率统计只能从统一 Registry 派生。
3. Agent Skill 不再维护具体页面类型清单，改为使用运行时注入的 Catalog 上下文。
4. 页面类型的分类、运行时特征、路由映射和正式导航别名均可由 Catalog 校验和查询。
5. Catalog 版本及内容哈希进入任务审计链路。
6. 新增页面类型原则上只需修改 Catalog，不再同步修改完整 Python 集合或 Skill 清单。

## 3. 非目标

- 本期不引入后台动态编辑页面类型。
- 本期不把数据库改为页面类型权威来源。
- 本期不支持 Catalog 运行时热更新。
- 本期不重写页面识别算法。
- 本期不消除所有针对单个页面类型的业务分支；仅消除重复分类清单和可配置映射。
- 本期不引入复杂的类型继承或规则表达式语言。

## 4. 方案选择

### 4.1 采用方案：运行时 PageTypeRegistry

服务启动时从 JSON 创建不可变 Registry，所有消费者通过 Registry 查询页面类型及其派生
集合。Agent 上下文在运行时从同一 Registry 生成。

优点：

- 不产生需要提交的生成文件；
- 修改 Catalog 后不存在遗漏同步文件的问题；
- 适合当前配置化、分层单体架构；
- 可集中完成引用校验、版本校验和审计。

### 4.2 未采用方案：构建时生成

从 JSON 生成 Python 常量和 Skill 片段。该方案运行时简单，但生成物会成为新的漂移风险，
必须额外依赖 CI 检查生成物是否最新，不符合减少重复出口的目标。

### 4.3 未采用方案：数据库权威目录

数据库动态配置适合未来运营后台，但会与 JSON 唯一事实源约束冲突，也会引入版本发布、
缓存一致性和任务快照等额外复杂度，当前阶段不采用。

## 5. 总体架构

```text
page_type_catalog.json
        |
        v
PageTypeRegistry
        |-- all_codes
        |-- business_codes
        |-- lifecycle_codes
        |-- coverage_required_codes
        |-- rule_matchable_codes
        |-- transient_codes
        |-- require()
        |-- resolve_alias()
        |-- resolve_route_prefix()
        |-- default_detail_type()
        `-- build_agent_context()
                 |
                 +--> API / Runtime / Agent / Exploration / Reporting
```

`PageTypeRegistry` 是领域层唯一读取和解释页面类型目录的出口。消费者不得自行读取 JSON，
也不得声明完整 canonical code 集合。

## 6. Catalog 数据模型

Catalog 顶层增加明确的 Schema 版本：

```json
{
  "schema_version": "page-type-catalog.v2",
  "version": "2026-07-18.v8",
  "page_types": []
}
```

每个页面类型增加分类和运行时元数据：

```json
{
  "code": "video_list",
  "category": "business",
  "rule_matchable": true,
  "coverage_required": true,
  "runtime_traits": [],
  "route_prefixes": ["movie", "movies", "video", "videos"],
  "navigation_aliases": [
    {
      "subtype": "long_video",
      "terms": ["视频", "影视", "片库", "movie"],
      "source_category": "视频内容",
      "confidence": 0.95
    }
  ],
  "default_detail_page_type": "video_detail"
}
```

生命周期页面示例：

```json
{
  "code": "line_loading",
  "category": "lifecycle",
  "rule_matchable": false,
  "coverage_required": false,
  "runtime_traits": ["transient"]
}
```

字段语义：

- `category`：`business` 或 `lifecycle`；
- `rule_matchable`：是否允许进入候选规则匹配；
- `coverage_required`：是否进入业务页面完整覆盖评估；
- `runtime_traits`：运行时通用行为标签，首期支持 `transient`；
- `route_prefixes`：可确定性映射到该类型的 URL 路由前缀；
- `navigation_aliases`：正式、已审核的导航别名及可选子类型；
- `default_detail_page_type`：列表页默认详情类型；
- 现有 `signals`、`entry_hints`、`next_page_candidates` 等字段继续保留。

不为纯算法实现细节新增 Catalog 字段。只有多个消费者共同需要、且属于页面类型稳定语义的
信息才进入 Catalog。

## 7. PageTypeRegistry

建议新增
`services/api/app/domain/page/registry.py`，保持不可变、可缓存、可注入。

主要接口：

```python
registry.all_codes
registry.business_codes
registry.lifecycle_codes
registry.coverage_required_codes
registry.rule_matchable_codes
registry.transient_codes

registry.require(code)
registry.resolve_alias(label)
registry.resolve_route_prefix(prefix)
registry.default_detail_type(code)
registry.build_agent_context(scope)
```

职责边界：

- Registry 负责加载、解析、分类、查询、引用校验、版本及哈希计算；
- Registry 不负责 DOM 分析、截图判断、探索排序或浏览器操作；
- 页面识别和探索代码只消费 Registry 输出；
- `load_default_page_type_catalog()` 暂时保留为兼容入口，内部委托默认 Registry；
- 默认 Registry 在进程内只初始化一次，测试可注入独立 Registry。

## 8. 消费者改造

### 8.1 探索与覆盖

- `exploration_tools._BUSINESS_PAGE_TYPES`
  替换为 `registry.coverage_required_codes`；
- `page_exploration_summary._LIFECYCLE_PAGE_TYPES`
  替换为 `registry.lifecycle_codes`；
- 业务覆盖率分母统一使用 `coverage_required_codes`；
- Catalog 顺序作为 API、覆盖率和 Agent 上下文的稳定展示顺序。

### 8.2 运行时状态

- `page_inspection_memory.TRANSIENT_PAGE_TYPES`
  替换为 `registry.transient_codes`；
- 可复用的类型分类行为优先通过 `runtime_traits` 表达；
- 针对单个页面结构的算法分支可以保留，但输入 code 必须已经通过 Registry 校验。

### 8.3 页面探索映射

- `page_exploration_profile._expected_page_type_from_label()`
  改为调用 `registry.resolve_alias()`；
- `nav_alias.DEFAULT_PAGE_NAV_ALIAS_TERMS`
  改为从 Catalog 的 `navigation_aliases` 构建；
- `page_detection_tools._BUSINESS_ROUTE_PAGE_TYPES`
  改为 `registry.resolve_route_prefix()`；
- 列表到详情的默认映射改为 `registry.default_detail_type()`；
- `entry_hints` 和 `next_page_candidates` 已能表达的关系，不再另建 Python 集合。

### 8.4 API 与规则

- 规则创建、任务查询、候选规则查询继续通过 Registry 的严格 code 校验；
- 工作台页面配置接口从 Registry 返回页面类型；
- API 不在 Pydantic Schema 中复制静态枚举，避免 OpenAPI 定义与运行时目录漂移；
- 若前端需要下拉选项，统一调用页面配置接口，不维护本地枚举。

## 9. Agent 动态上下文

Main Agent 初始化和 Skill 加载阶段注入紧凑上下文。以下内容为字段格式示例，运行时必须写入 Registry 计算出的真实 SHA256：

```json
{
  "catalog_version": "2026-07-18.v8",
  "catalog_sha256": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "all_page_types": ["line_loading", "home", "content_list", "content_detail", "ranking", "profile", "settings", "payment", "post_list", "post_publish", "activity", "startup_ad", "video_list", "video_detail"],
  "coverage_required_page_types": ["home", "content_list", "content_detail", "ranking", "profile", "settings", "payment", "post_list", "post_publish", "activity", "video_list", "video_detail"],
  "lifecycle_page_types": ["line_loading", "startup_ad"],
  "rule_matchable_page_types": ["home", "content_list", "content_detail", "ranking", "profile", "settings", "payment", "post_list", "post_publish", "activity", "startup_ad", "video_list", "video_detail"]
}
```

改造原则：

- `instructions.py` 删除 12 个业务类型的具体清单；
- `qc-workflow/SKILL.md` 改为要求覆盖
  `coverage_required_page_types`；
- `page-detection/SKILL.md` 只解释 canonical code 和分类字段语义；
- 页面识别 Tool 继续接收完整 Catalog，其他阶段只注入所需的紧凑视图；
- Agent 输出中的 page type 必须属于注入上下文；
- Prompt 中附带 Catalog 版本和 SHA256，便于运行审计。

## 10. 导航别名数据库定位

`page_nav_alias_term` 和向量表调整为候选与索引投影，不再是平行权威源：

- JSON 中的 `navigation_aliases` 是已确认、可执行的正式定义；
- 数据库保存采集到的候选别名、审核状态和向量索引；
- 未进入 JSON 的候选别名不能直接改变 canonical page type 判定；
- 候选审核通过后通过代码变更写入 JSON，再同步数据库正式索引；
- 数据库中的 `canonical_code` 必须通过 Registry 校验；
- 部署或启动同步任务可以根据 JSON 重建正式别名索引。

数据库内容是运行投影和审计数据，不是页面类型配置源。

## 11. 校验与错误处理

Registry 初始化时执行以下强校验：

- `code` 符合 `^[a-z][a-z0-9_]*$` 且全局唯一；
- `category` 只能为 `business` 或 `lifecycle`；
- `coverage_required=true` 只能用于业务类型；
- `transient` 类型不能要求业务覆盖；
- `default_detail_page_type` 必须引用现有类型；
- `entry_hints.from_page_type` 除 `external` 外必须存在；
- `next_page_candidates.page_type` 必须存在；
- 路由前缀不能冲突映射到不同 canonical code；
- 同一正式导航别名不能以相同优先级映射到不同 canonical code；
- 所有 Agent 上下文类型必须来自 Registry；
- 数据库正式别名投影中的 canonical code 必须存在。

Catalog 无效时服务启动失败，不允许静默忽略错误或降级为空目录。请求级非法 code 继续返回
明确的业务错误，并携带允许值或 Catalog 版本，便于定位。

## 12. 版本与审计

Registry 对规范化后的 Catalog 内容计算 SHA256。每个任务或 Agent Run 记录以下字段；示例摘要不作为固定值：

```json
{
  "catalog_version": "2026-07-18.v8",
  "catalog_sha256": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

页面识别结果继续记录 Catalog 版本，并补齐相同内容哈希。Catalog 原文快照可以作为审计
附件保存，但不得成为可编辑配置源。

同一 Agent Run 不允许在无记录情况下切换 Catalog。恢复旧 Run 时若发现哈希变化，必须
创建新 Run 或写入明确的版本切换事件，禁止静默用新语义续跑旧 Run。

Catalog 变化后通过服务重启生效，本期不实现热更新。

## 13. 分阶段迁移

### 阶段一：Registry 基础

- 扩展 Catalog Schema；
- 新增不可变 Registry；
- 保留旧加载函数兼容入口；
- 增加完整性和引用校验测试；
- 不改变现有业务行为。

### 阶段二：运行时代码迁移

- 迁移探索收口、覆盖率、生命周期和瞬态集合；
- 迁移路由、导航别名和默认详情映射；
- 删除对应 Python 重复集合；
- 保留必要的单类型算法分支。

### 阶段三：Agent 上下文迁移

- 删除 `instructions.py` 和 Skill 中的具体类型清单；
- 在 Agent 初始化及 Skill 加载层注入 Catalog Context；
- Tool 参数描述和输出校验统一使用 Registry；
- 验证 Prompt 版本和哈希可追溯。

### 阶段四：数据库投影与清理

- 正式导航别名由 JSON 同步到数据库索引；
- 数据库非 Catalog 别名统一降为候选状态；
- 删除旧兼容常量和无用映射；
- 增加静态防回归检查。

## 14. 测试方案

### 14.1 单元测试

- Registry 分类派生结果正确；
- 不存在的跨引用会阻止初始化；
- 重复 code、路由冲突和别名冲突会被发现；
- `resolve_alias()`、`resolve_route_prefix()` 与迁移前行为一致；
- Agent Context 的内容、顺序、版本和 SHA256 正确；
- 生命周期、瞬态、覆盖范围和规则可匹配范围派生正确。

### 14.2 契约测试

- 探索收口要求的类型等于 `coverage_required_codes`；
- 页面识别输出只能使用 `all_codes`；
- 规则 API 的 page type 必须通过 Registry 校验；
- 工作台页面配置接口顺序与 Catalog 顺序一致；
- 数据库正式别名的 canonical code 全部存在于 Registry；
- Agent Skill 加载结果不包含独立维护的完整清单。

### 14.3 回归测试

- 完整执行 `test_page_type_catalog.py`；
- 完整执行 `test_page_detection_tools.py`；
- 执行探索收口、覆盖率和工作内存相关测试；
- 执行规则创建和候选规则查询相关测试；
- 执行 Agent Prompt 与 Skill 加载测试。

### 14.4 静态防回归

- 禁止重新声明 `_BUSINESS_PAGE_TYPES`、`_LIFECYCLE_PAGE_TYPES`、
  `TRANSIENT_PAGE_TYPES` 等完整集合；
- 禁止 Skill 和 `instructions.py` 维护完整 canonical code 清单；
- 允许特定算法引用单个 canonical code；
- 当多个模块需要同一种分类行为时，必须迁入 Catalog 元数据或 Registry 查询。

## 15. 验收标准

1. 新增页面类型只需编辑 `page_type_catalog.json`。
2. 无需同步修改 Python 类型集合、Main Agent 指令或 Skill 清单。
3. API、页面识别、探索、覆盖统计和规则匹配使用同一 Registry。
4. 无效跨引用在启动或 CI 阶段失败。
5. 每次任务能够追溯 Catalog 版本与内容哈希。
6. 数据库导航别名不会成为独立 canonical 定义源。
7. 现有页面识别、探索收口和规则匹配行为无回归。
8. Catalog 顺序在 API、报告和 Agent 上下文中保持稳定。

## 16. 风险与控制

- **Catalog Schema 升级影响范围较大**：分阶段迁移并保留旧加载函数。
- **Prompt 动态注入遗漏**：通过 Agent Context 契约测试覆盖所有加载入口。
- **数据库历史别名改变识别结果**：先区分正式定义与候选数据，再迁移读取优先级。
- **类型特征过度配置化**：只将稳定且被多个消费者共享的语义放入 Catalog。
- **旧任务跨版本恢复不确定**：记录版本和哈希，禁止无审计的静默切换。
- **一次性改造导致回归定位困难**：每阶段保持可独立验证和回滚。
