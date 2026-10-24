# domain/rule 规则与数据源域

对应设计文档中"质检规则数据源"与相关独立数据源，是整个平台质检能力的地基。

## 核心实体

- `qc_ruleset`：规则集（一次任务锁定的规则版本快照单位）。
- `qc_rule`：质检规则。关键字段：
  - `category`：细化为 8 类（`text`/`visual`/`interaction`/`ad`/`payment`/`input_risk`/`player`/`compliance`），详见[整合方案设计 3.1 节](../../../../../docs/design/2026-07-03-alioth-integrated-solution-design.md)。
  - `executor_type`：`llm` / `rule_engine` / `hybrid`（三态判定契约）。`rule_engine` 规则只有在 `judge_config.engine_config` 配置白名单 Handler 后才能由 `rule_judge_tool` 自动执行；当前试点为 alioth_6/14/41。`llm/hybrid` 结论由规则写回服务校验统一结构，hybrid 必须包含 Tool 结果摘要，低于 `manual_review_policy.min_confidence` 的结论自动转人工复核。
  - `steps`：可选，到达检查点所需的页面操作步骤。
  - `check_point`：自然语言判定标准。
  - `scope` / `url_pattern`：适用范围。
- `sensitive_word`：敏感词库（词条、类别、匹配方式、白名单例外）。
- `brand_term`：品牌词库（标准写法、禁用写法、大小写规范）。
- `knowledge_doc`：知识库文档，配合 pgvector 做向量检索。

## 职责

- 规则、敏感词、品牌词、知识库的增删改查校验规则（业务人员在管理后台维护，AI 侧不直接读写，须经由 `agents/tools` 的数据类 Tool 取数）。
- 规则集版本锁定逻辑：任务启动时生成不可变的规则集快照。
- 规则适用范围匹配逻辑（`scope`/`url_pattern` 过滤）。

## 关联

- 被 [domain/task](../task/README.md) 引用（`qc_task` 关联 `qc_ruleset`）。
- 被 [domain/evidence](../evidence/README.md) 引用（`qc_rule_execution` 关联 `qc_rule`）。
- 被 `agents/skills/rule/` 消费（规则 Skill 按 `category` 分组读取判定标准）。
