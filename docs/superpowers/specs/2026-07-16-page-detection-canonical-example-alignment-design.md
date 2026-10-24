# page-detection canonical 示例对齐设计

## 目标

按方案 A 修正 `page-detection` 与 `page-exploration` 中已经失效的页面类型示例，使所有结构化页面类型字段只输出当前页面目录的 14 个 canonical code；证据不足时使用 `page_type: null`。

## 修改范围

- 修改 `services/api/app/agents/skills/phase/page-detection/SKILL.md`。
- 修改 `services/api/app/agents/skills/phase/page-exploration/SKILL.md`。
- 按 AGENTS.md 要求更新 `docs/专项能力与高频回归规则.md`，记录本次契约漂移的防回归规则。
- 不修改页面类型目录、目录加载器、Agent 上下文注入、`candidate_rules_tool` Schema、数据库或前端。
- 不处理工作区中现有的 `page_detection_tools.py` 原型及其测试。

## Canonical 页面类型

结构化字段 `page_type`、`expected_page_type`、`expected_next_page_type` 只能使用：

`line_loading`、`home`、`content_list`、`content_detail`、`ranking`、`profile`、`settings`、`payment`、`post_list`、`post_publish`、`activity`、`startup_ad`、`video_list`、`video_detail`。

中文页面名称只用于说明和 `reason`，不能写入上述结构化字段。证据不足时不使用 `unknown` 伪类型，而是输出 `page_type: null` 与 `page_state: needs_more_observation`。

## 示例映射

- `默认首页` → `home`
- `广告推荐页` → 删除该候选；没有观测依据时不得机械替换成另一个 canonical code
- `视频列表页` → `video_list`
- `社区列表页` → `post_list`
- `detail` → `content_detail`

## 验收

1. 修改前的契约断言能够检出两份 Skill 中的非法结构化页面类型值。
2. 修改后，同一断言确认所有结构化页面类型值都属于当前目录的 14 个 code。
3. `page-detection` 的页面类型建议完整列出 14 个 canonical code，不保留旧的通用分类。
4. 专项回归规则记录 Skill 示例与目录同步要求。
5. Git 差异确认没有修改目录、Tool、Schema、数据库或前端文件。

