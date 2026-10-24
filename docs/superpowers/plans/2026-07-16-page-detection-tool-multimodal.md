# page_detection_tool 多模态识别实现计划

> 状态：执行中。对应设计：`docs/superpowers/specs/2026-07-16-page-detection-tool-multimodal-design.md`。

**目标：** 在页面采集后、规则匹配前，通过 `page_detection_tool(page_capture_id)` 将归属当前任务的完整 DOM 与截图连同固定 page-detection Skill、页面目录发送给独立多模态模型，并以结构化结果控制 workflow 分支。

**架构：** 本期采用已确认的单文件原型。证据查询、对象存储读取、DOM 清洗分块、提示词构造、多模态模型调用和返回校验全部放入 `services/api/app/agents/tools/page_detection_tools.py`，不新增 Service。Tool 只接收 `page_capture_id`，`task_id` 从 AgentRunContext 获取。

## 任务 1：用测试固定 Tool 契约

- 重写 `services/api/tests/test_page_detection_tools.py`。
- 覆盖单参数注册、任务归属、完整 DOM 分块、真实图片 Content、结构化 response format、目录校验、缺证据/模型错误门禁和紧凑返回。
- 先运行目标测试，确认新契约在旧实现上失败。

## 任务 2：实现单文件多模态 Tool

- 重写 `services/api/app/agents/tools/page_detection_tools.py`。
- 使用数据库读取 `qc_page_capture` 与 `qc_evidence_artifact`，查询同时约束 `task_id` 和 `page_capture_id`。
- 按本地/MinIO 后端读取 DOM 与所有截图；MinIO 依赖延迟导入。
- 清洗并分块完整 DOM，截图使用 Agent Framework `Content.from_data(..., media_type="image/png")`。
- 加载固定 Skill 与 canonical 页面目录，调用配置的 Chat Client，并通过 Pydantic response format 获取结构化结果。
- 仅 `recognized` 且页面类型属于目录时设置 `rule_matching_allowed=true`。

## 任务 3：调整 workflow 与 page-detection Skill

- 修改 `services/api/app/agents/skills/qc-workflow/SKILL.md`：步骤 3.3 调用 Tool，按四类状态分支后才可进入步骤 3.4。
- 修改 `services/api/app/agents/skills/phase/page-detection/SKILL.md`：它负责指导调用与解释契约，不再由 Main Agent 直接基于摘要识别。
- 更新 Tool/Skill README 或能力清单中受影响的说明。

## 任务 4：消除冲突设计并沉淀回归规则

- 将旧 Skill-only 设计和计划标记为已被本方案取代，不删除历史内容。
- 更新 `docs/专项能力与高频回归规则.md`，记录“完整证据留在嵌套模型调用内、Tool 返回紧凑结果”的固定边界。

## 任务 5：验证与独立复核

- 运行 page-detection 目标测试、工具注册/能力清单测试及相关 Skill 契约测试。
- 运行 Ruff 或项目现有静态检查。
- 审核修改文件、未修改的用户脏文件、真实图片输入和完整 DOM 输入的代码证据。
- 由独立审查代理检查 tools、Skill、workflow 和安全门禁，修正高优先级问题后再交付。
