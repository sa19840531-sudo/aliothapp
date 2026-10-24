# page_type 目录按附件对齐设计

## 目标

以附件 `质检平台_综合方案表 (3).html` 第四节“page_type 词表（17 值）”为唯一权威来源，调整 `services/api/app/domain/page/page_type_catalog.json`，消除当前目录与综合方案的页面类型差异。

## 范围

- 只修改 `page_type_catalog.json`。
- 保留当前 JSON 的字段结构，避免在本次任务中连带迁移加载器、Agent Tool、数据库或前端。
- 不覆盖工作区中已有的未提交文件。

## 权威 page_type

目录最终只保留以下 17 个 `code`：

`global`、`general`、`toast`、`content_list`、`content_detail`、`ranking`、`profile`、`settings`、`payment`、`comment`、`post_list`、`post_publish`、`activity`、`ad_popup`、`startup_ad`、`pre_roll_ad`、`player`。

其中 `global` 是全页面通用规则标签；`line_loading`、`unknown` 以及旧目录中的业务细分类型不再作为目录中的独立 page_type。

## 数据调整规则

- `code` 严格使用附件中的英文值，不增加兼容超集。
- `name` 使用附件对应的中文含义。
- `aliases` 至少包含英文 `code`，保证现有加载器的 `require()` 能按英文 page_type 查询。
- `status` 统一设为 `supported`，因为 17 个值均为当前正式词表。
- `signals` 只保留可由附件含义直接推导的基础识别提示，不沿用与新词表冲突的旧页面细分。
- `entry_hints`、`exit_hints`、`next_page_candidates`、`related_rules` 不凭空补写附件未定义的关系；无可靠依据时使用空数组。
- `source` 元数据改为附件名称和对应章节，目录版本更新为本次调整日期。

## 验证

1. JSON 能被标准解析器读取。
2. `page_types` 数量等于 17，`code` 集合与附件完全一致且无重复。
3. 当前目录加载器能够加载文件，并能通过英文 page_type 查询 17 个条目。
4. 运行现有目录相关测试并如实记录结果；工作区里已存在的未提交 V2 测试与本次单文件范围冲突，不为迎合该测试而偏离附件。

## 非目标与风险

- 本次不修复未完成的 V2 `catalog.py` / `page_detection_tools.py` 契约。
- 本次不调整规则种子数据中的 page_type。
- 本次不引入数据库迁移，也不需要同步前端菜单或路由。
- 若后续要求相关 V2 Tool 与测试全部通过，应另行统一加载器模型、Tool 输出契约和测试基线。
