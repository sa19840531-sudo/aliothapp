# sensitive_word_detection_tool 设计说明

> 日期：2026-07-10
> 范围：本轮接入本地静态禁止内容库，提供可审计穷举扫描；不新增数据库迁移。

## 目标

`sensitive_word_detection_tool` 对采集到的文本执行禁止内容库扫描，返回命中词、分类、位置和白名单状态。该 Tool 服务 `text` / `input_risk` / `compliance` 等规则，作为 `rule_engine` 或 `hybrid` 判定的确定性前置结果。

## 分类

禁止内容库固定对齐 8 类：

- `political`：政治
- `terrorism`：恐怖
- `personal_attack`：人身攻击
- `china_related`：涉华
- `adult`：成人
- `gambling`：赌博
- `private_ad`：私人广告
- `phishing`：钓鱼诱导

其中“政治 / 恐怖 / 人身攻击”对应现有首个合并类规则的三个子项，落库前先在词库分类中拆开，方便规则结果审计和后续人工复核。

## 分层

| 层级 | 目录 | 职责 |
| --- | --- | --- |
| 规则数据源域 | `services/api/app/domain/rule/forbidden_content.py` 与 `forbidden_content_catalog.json` | 定义禁止内容分类、词条、白名单和扫描算法 |
| Tool 层 | `services/api/app/agents/tools/sensitive_word_tools.py` | 注册 `sensitive_word_detection_tool`，调用扫描服务，返回结构化结果 |

## 输出契约

返回字段：

- `execution_status`：固定为 `success`
- `catalog_version`
- `text_length`
- `hit_count`
- `effective_hit_count`：排除白名单后的有效命中数
- `hits[]`：`term`、`category`、`category_label`、`start`、`end`、`match_type`、`is_whitelisted`

## 不做范围

- 不做 LLM 语义判断。
- 不做数据库 CRUD。
- 不直接写规则执行结果。
- 不把词库硬编码在 Tool 函数内。
