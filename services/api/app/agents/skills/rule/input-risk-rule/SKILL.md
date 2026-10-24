---
name: input-risk-rule
description: 用户产生内容与输入风险质检 Skill。适用于 candidate_rules_tool 返回 category=input_risk，且检查点涉及已发布 UGC 的敏感内容、恶意链接、引流、重复刷屏、防滥用机制或上传规则展示等场景；指导 Main Agent 使用 page_capture_tool、sensitive_word_detection_tool、qr_link_validation_tool、deterministic_assertion_tool、rule_judge_tool 与结果写回 Tool，不内置规则编号或词库判定标准。
---

# input-risk-rule

## 职责与适用场景

仅处理 `candidate_rules_tool` 返回的 `category=input_risk` 候选规则，适合：

- 评论、帖子、昵称、签名等已发布 UGC 的敏感或禁止内容扫描。
- 已发布文本、二维码或 URL 中的恶意链接、钓鱼或站外引流风险。
- 重复内容、刷屏与页面已展示的防滥用结果观测。
- 发布页已展示的上传限制、格式、数量或选择规则说明。
- 需要词库扫描、链接校验、确定性计数与语义复核组合的风险质检。

具体禁止类别、白名单、重复阈值、例外、上传限制、严重度和复核策略由数据库候选规则定义。Skill 只提供安全采证与 Tool 编排，不保存规则编号或业务口径。

平台自有文案质量交给 `text-rule`；普通交互反馈交给 `interaction-rule`；商业广告页面行为交给 `ad-rule`。

## 最高安全边界

- 默认只读已发布内容，绝不制造、输入或发布违规内容来测试过滤器。
- 不使用真实个人信息，不上传文件，不扫码，不在质检浏览器中直接访问可疑链接。
- 只有候选规则 `execution_route.handler` 明确要求防滥用探测，且处于隔离测试环境、专用账号和可清理内容区时，才允许按配置执行有界中性探针；否则保持只读。
- 当前版本不主动执行防滥用 `submit_probe`；规则需要探针而现有证据缺失时写 `evidence_insufficient`。未来启用后也只能使用受控技术前缀并严格限制次数，任何违规内容、持续灌水、批量发布或绕过限制的验证都禁止执行。

## 动态规则输入

逐条读取并保留：

- `rule_id`、`rule_code`、`name`、`severity` 与适用页面信息。
- `check_point`、`steps`、`input_contract`、`judge_config`、`manual_review_policy`。
- `execution_route`、所需输入、证据类型和 Tool 路由。

不得用 Skill 中的场景名称推导实际禁止项。词库类别、白名单、阈值和例外必须来自数据库规则或 Tool 的受控数据源。

## 可用 Tool

| Tool | 输入风险质检用途 | 使用条件 |
| --- | --- | --- |
| `page_capture_tool` | 读取当前页面采集已有的 UGC、页面说明、DOM、截图和动作轨迹 | 当前版本页面探索不主动调用 `page_capture_tool` 补采，也不执行 `submit_probe` |
| `sensitive_word_detection_tool` | 扫描文本并返回命中词、类别、位置和白名单状态 | 候选规则检查点涉及禁止内容或敏感内容时；Tool 结果是证据，不替代规则例外 |
| `qr_link_validation_tool` | 解码二维码并校验 URL 可达性、黑名单或异常跳转 | 候选规则涉及链接或二维码时；浏览器不直接访问可疑地址 |
| `deterministic_assertion_tool` | 文本、正则、DOM 存在性、重复计数或数值关系的辅助断言 | 模式、操作符与阈值必须来自 `judge_config` |
| `rule_judge_tool` | 执行单条已就绪的确定性风险规则 | 仅按 `execution_route` 调用 |
| `rule_judge_batch_tool` | 批量执行同一页面已就绪的确定性风险规则 | 多条规则时优先使用 |
| `record_rule_execution_tool` | 写回单条候选或补偿失败项 | 动态规则标识原样使用 |
| `record_rule_executions_batch_tool` | 批量写回同一页面多条结果 | 多规则页面优先使用 |

## 执行流程

1. 从 `execution_plan.batches` 取得 input_risk 批次，读取数据库规则定义与证据契约。
2. 确认质检对象是候选规则要求的已发布 UGC 或页面说明；页面无对应对象时按规则语义写 `skipped`。
3. 只读取当前 `page_capture_id` 已有的文本、DOM、截图和附件 ID，并记录抽样范围。抽样结果不得宣称代表未覆盖的全量内容。
4. 规则涉及敏感内容时调用 `sensitive_word_detection_tool`；涉及链接或二维码时调用 `qr_link_validation_tool`；涉及计数或存在性时按规则配置调用 `deterministic_assertion_tool`。
5. 需要语义复核时，仅依据 `check_point`、Tool 命中、上下文和规则声明的白名单或例外形成结构化判断；不自行扩张禁止范围。
6. 规则要求中性触发或连续防滥用探针但现有证据未包含完整 Action Trace、HAR 与采集角色时，写 `evidence_insufficient`；不得主动执行 `submit_probe`。
7. `execution_route.rule_engine_ready=true` 时使用统一判定 Tool；其他策略按公共 `llm/hybrid` 结构输出。
8. 从 `manual_review_policy` 读取置信度和高风险复核条件，再通过写回 Tool 保存结论与证据。

## 场景化 Tool 组合

- **敏感内容**：`page_capture_tool` → `sensitive_word_detection_tool` → 按白名单和语境结构化复核 → 写回。
- **恶意链接或二维码**：只读提取 → `qr_link_validation_tool` → 必要的词库或语义复核 → 写回。
- **重复或刷屏**：只读抽样 → 复用已有 Action Trace、HAR、限频文案和最终可见重复数 → 写回；证据不完整时写 `evidence_insufficient`。
- **上传规则展示**：只读采集页面说明 → DOM/文本断言 → 写回；不打开系统文件选择器、不上传文件。

## 证据与复核边界

- 词库或专用链接 Tool 不可用且规则依赖该能力时，不得仅凭模型给出确定性通过。
- 文本提取、截图或动作后观测缺失时写 `evidence_insufficient`；语境或例外无法确认时按数据库策略转人工复核。
- 敏感截图与文本只保留必要、脱敏的证据引用；不在 `judge_output` 复制大段违规内容。
- 受控探针原文和故障参数不得进入持久化动作审计；审计只保留长度与 SHA256。空 HAR 表示已完成网络观测但没有事件，缺少 HAR 才是证据不完整。
- 每条动态候选规则独立写回，即使多条规则复用同一次扫描结果也不合并规则身份。

## 交接

- 上游：`rule-matching` 与 `candidate_rules_tool`。
- 下游：`evidence-report` 与规则结果写回 Tool。
- 公共执行与写回契约以 [rule-matching](../rule-matching/SKILL.md) 为准。
