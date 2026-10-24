# agents/tools Tool（Function Calling）

可执行函数，真正产生副作用或返回结构化数据，等价于 Agent Framework 里挂载给 Agent 的 `Tool`（function calling）。

与遵循 [Agent Skills 规范](https://agentskills.io)（`SKILL.md`、渐进式披露）的 [../skills/](../skills/README.md) 是两个不同的概念：Tool 是"调用即执行"的函数契约，Skill 是"读取以决策"的自然语言知识文档，二者的区别与命名沿革详见 [../README.md](../README.md)。

## 规划的 Tool 清单

| Tool | 输入 | 输出 | 说明 |
| --- | --- | --- | --- |
| page_capture_tool | 模式（goto/observe/timed_capture/single_trigger）、URL、选择器、受控探针/故障参数、采集参数 | 执行状态、页面摘要、page_capture_id、artifact_ids | Agent 可调用的统一页面采集入口；新 URL 必须使用 goto，observe/timed_capture/single_trigger 仅作用于当前会话页面；single_trigger 的 trigger_action 只填写动作枚举，click/hover 目标写入 selector，network_fault 使用限定的 fault_url_pattern，submit_probe 使用专用中性文本和提交选择器；可信弹窗关闭动作可由运行时注入 `clear_obstructions`，在 12 层/45 秒内逐层重新证明关闭目标并留存截图与动作轨迹；完整 DOM、截图和日志进入证据存储，Tool 只返回摘要与证据 ID |
| page_detection_tool | page_capture_id | page-detection.v2 结构化识别结果与规则匹配门禁 | 内部按当前 task_id 读取完整语义 DOM、全部截图及可选 OCR，加载固定 Skill/目录并调用独立多模态模型；不向 Main Agent 返回原始证据 |
| candidate_rules_tool | Catalog 标准 page_type、page_capture_tool 返回的 page_capture_id（均必填） | 当前任务锁定规则集下的候选规则、execution_plan 与 WorkingMemory 页面进度 | Agent 内部按 applicable_page_types 查询候选规则，并按 strategy/category 生成批量执行顺序；不暴露 task_id/ruleset_id 给模型 |
| exploration_closure_tool | 关闭原因、动作终态、探索动作 ID/全部待处理动作、页面采集 ID/全部待处理页面、产品动态页面范围及评估 | 动作与页面收口结果、WorkingMemory 页面/Frontier 进度及页面类型评估 | 将风险、抽样或预算限制下不再执行的探索项写入机器可读终态；报告前以 `assessment_scope_page_types` 固化产品动态分母并提交完整 `page_type_assessments`；禁止关闭已经开始但未完成规则写回的页面 |
| deterministic_assertion_tool | assertion_type、payload | pass/fail、摘要、命中详情 | 正则、文本包含、数值比较、DOM 存在性等确定性断言 |
| rule_judge_tool | rule_code、page_capture_id、evidence_artifact_ids、evidence_capture_roles、judge_input | 统一 judge_strategy、execution_status、confidence、observation_packet_id、judge_input/output | 按任务锁定规则的 engine_config 执行确定性判定；后端校验证据归属、类型、摘要和采集角色并创建 Observation Packet；二维码规则由后端从可信文本、UI 快照和截图提取目标，不要求 Agent 手工复制 URL |
| rule_judge_batch_tool | page_capture_id、evidence_artifact_ids、evidence_capture_roles、rules | 多条确定性规则的逐项判定结果 | 同一页面或同一组多采集证据共享引用并批量执行 ready 的 rule_engine 规则，减少模型往返 |
| qr_link_validation_tool | 二维码截图或 URL | 解码结果、可达性、浏览器可访问性、跳转异常 | 用于 alioth_41 等二维码/网址有效性校验 |
| sensitive_word_detection_tool | 待检测文本 | 命中词、分类、位置、是否白名单 | 基于禁止内容库做政治/恐怖/人身攻击/涉华/成人/赌博/私人广告/钓鱼诱导 8 类穷举扫描，降低 LLM 漏判并保留审计依据 |
| 品牌词检测 Tool | 待检测文本 | 命中词、标准写法、是否违规、位置 | 同上 |
| 知识库检索 Tool | query、top_k | 相关知识片段及相似度得分 | pgvector 向量检索 |
| OCR Tool | 图片 | 识别出的文字内容 | 用于图片内文案类规则 |
| 截图比对 Tool | 两张截图 | 差异区域、差异程度 | 用于视觉类规则 |
| 证据请求 Tool（V2+V3） | 证据类型、区域、页面引用 | evidence_request | 生成补采请求 |
| AI 判断 Tool（V2+V3） | Observation Packet、规则契约 | 结构化判断、置信度、理由 | 经 AI Gateway 调用 LLM/VLM，强制 JSON Schema |
| record_rule_execution_tool | rule_id/rule_code、execution_status、page_capture_id、observation_packet_id、summary、evidence_artifact_ids、judge_input/output、confidence | 写入后的 execution_id、规则状态与 WorkingMemory 页面进度，或结构化 blocked | 统一收口写 `qc_rule_execution`；确定性判定关联并校验 Observation Packet，llm/hybrid 使用统一结构化输出，低置信度自动转人工复核，重载荷仅通过 artifact_id 引用 |
| record_rule_executions_batch_tool | page_capture_id、executions | 逐项写入结果、成功/失败计数与 WorkingMemory 页面进度 | 同一页面共享 page_capture_id 批量写回；逐条复用原校验与幂等键，部分失败时只补偿失败条目 |
| report_generation_tool | 当前 Agent Run 上下文 | 含候选执行完整性的结构化质检报告 | 循环结束时调用一次，不允许模型传任意 task_id；候选规则未写回时状态为 incomplete |

详见[Agent Loop 编排设计 2.1 节](../../../../../docs/design/2026-07-01-alioth-agent-loop-design.md)与[最终整合方案设计 5.6 节](../../../../../docs/design/2026-07-03-alioth-integrated-solution-design.md)（历史文档中称为"工具型 Skill"，2026-07-05 起按 function calling 语义统一为 **Tool**，详见 [../README.md](../README.md)）。

## 约定

- Tool 是 Agent 可见的能力契约，具体实现委托给 [infrastructure/](../../infrastructure/README.md)，Tool 层只做入参校验、调用编排与输出结构化。
- `page_detection_tools.py` 是本期经确认的一次性单文件原型例外：证据查询/读取、DOM 分块、多模态请求与结果校验暂集中在该文件；契约稳定后再拆分仓储和应用服务。
- 每个 Tool 的输入输出建议用 JSON Schema 显式定义，便于 Agent Framework 做 function calling 校验，也是 V2+V3 阶段 Tool Registry 的数据来源。
- 高风险 Tool（页面操作中的支付/登录/下载类动作）需要标注 `permission_level`，供 `navigation-boundary` Skill 与审批策略约束。

## V1 实现（2026-07，2026-07-05 术语更正）

上表是完整规划清单，当前 V1 已落地"注册表框架 + 权限分级 + 规则闭环基础 Tool + 确定性断言 Tool + 报告生成 Tool + 页面采集 Tool + 页面识别 Tool + 敏感词检测 Tool"：

- `ocr_tool`、`screenshot_compare_tool` 属于 V1 首期范围，随“页面采集与证据 Tool 改造”任务实现；注册到 Capability Manifest 前，Agent 不得假设其可调用。
- `brand_word_detection_tool`、`knowledge_retrieval_tool` 不纳入 V1 首期，后续版本根据品牌词库和知识库数据源准备情况再实现。
- 未注册 Tool 不得出现在运行时可调用能力清单；对应规则在缺少必要证据时使用 `evidence_insufficient` 或 `manual_review`，不得伪造 Tool 结果。

| 模块 | 职责 |
| --- | --- |
| `app_device_tools.py` | Android 试点：`load_app_product_profile_tool`、`app_screenshot_tool`、`app_tap_tool`、`app_cloud_back_tool`、`app_cloud_back_twice_tool`、`app_tap_from_profile_tool`。需 `ALIOTH_ADB_SERIAL`；截图勿插在双 back 中间。 |
| `registry.py` | `PermissionLevel`（`safe`/`sensitive`）、`ToolMetadata`、`ToolRegistry`（`register`/`get`/`list_by_category`/`list_tools`）、`@tool(...)` 装饰器（内部调用 `agent_framework.tool` 包装成 `FunctionTool` 并登记元数据）。模块级单例 `default_registry`。 |
| `permissions.py` | `PermissionEnforcementMiddleware`（`FunctionMiddleware`）：越权调用**软阻断**（不调用 `call_next`、直接设置 `context.result` 为拒绝提示），不终止整个 Agent 循环，允许模型换一种方式继续任务。 |
| `rule_tools.py` | 真实规则业务 Tool：`candidate_rules_tool` 查询候选规则并生成 strategy/category 批次；单条与批量写回 Tool 复用同一 `RuleExecutionService` 校验和页面级幂等键。批量写回共享 `page_capture_id`、逐项返回成功或阻断结果；`pass`/`fail`/`warning`/`manual_review` 必须提供非空证据引用。 |
| `assertion_tools.py` | 确定性断言 Tool：`deterministic_assertion_tool` 提供正则、文本包含、数值比较、DOM 存在性等可复现校验能力，服务 `rule_engine` 类规则。 |
| `rule_judge_tools.py` | 单条与批量确定性规则判定 Tool：从 Agent Run 上下文绑定 `task_id/agent_run_id`，读取任务锁定规则的 `executor_type/engine_config`；证据包支持显式 `primary/after/refreshed/fault` 等采集角色，批量入口共享同一组证据并逐项返回，未配置规则和无效证据保持结构化阻断。 |
| `report_tools.py` | 报告生成 Tool：`report_generation_tool` 复用 `ReportGenerationService` 汇总当前任务详情与 `qc_rule_execution` 结果，并按当前 Run 的候选查询轨迹校验规则写回完整性；存在缺口时返回规则码并标记 `incomplete`。 |
| `page_capture_tools.py` | 页面采集 Tool：`page_capture_tool` 是 Agent 面向浏览器观测的统一入口，支持导航、只读观测、多帧定时采集和单次最小触发；任何新 URL 导航必须走 goto 并经过页数/深度门禁；只做契约、安全边界与结构化返回，Playwright 工程细节位于 `infrastructure/browser`。 |
| `page_detection_tools.py` | 页面识别 Tool：`page_detection_tool(page_capture_id)` 校验任务归属，读取完整语义 DOM/全部截图/可选 OCR，加载固定 Skill 与页面目录，向配置的多模态模型发送真实图片 Content；识别成功后按 task/run 归属回写 `qc_page_capture.page_type/page_state`，持久化失败时关闭规则匹配门禁。 |
| `exploration_tools.py` | 页面探索收口 Tool：校验动作和页面关闭请求，由 Run 级工作记忆中间件写入 `blocked`/`skipped`/`failed` 与页面 `abandoned` 终态。 |
| `sensitive_word_tools.py` | 敏感词检测 Tool：`sensitive_word_detection_tool` 复用 `domain/rule` 的禁止内容库，返回命中词、分类、位置和白名单状态；服务 `text`/`input_risk`/`compliance` 等规则的 `rule_engine` 或 `hybrid` 判定。 |

`ToolRegistry` 目前只在进程内存中生效（不落库、不跨进程共享），一次注册即可通过 `list_tools()` 直接传给 `agent_factory.build_main_agent(tools=...)`。调试接口 [../../api/v1/agents.py](../../api/v1/agents.py) 固定只放行 `safe` 级别 Tool。测试见 [../../../tests/test_tools_registry.py](../../../tests/test_tools_registry.py)、[../../../tests/test_tools_permissions.py](../../../tests/test_tools_permissions.py)。

> **2026-07-05 目录重命名说明**：本目录原名 `agents/skills/`，因与 [Agent Skills 规范](https://agentskills.io) 撞名而改名为 `agents/tools/`；原 `agents/playbooks/` 已更名为现在的 [../skills/](../skills/README.md)。详见 [docs/专项能力与高频回归规则.md](../../../../../docs/专项能力与高频回归规则.md)。
