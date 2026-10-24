# page_detection_tool 多模态识别设计

## 状态

已确认，取代早期 Skill-only 观察方案。

## 目标

在 workflow 的页面采集与规则匹配之间调用 `page_detection_tool(page_capture_id)`。Tool 依据当前 Agent 运行上下文校验任务归属，读取该次采集持久化的完整 DOM 与全部截图，加载固定的 `page-detection` Skill 和页面类型目录，发起独立多模态模型请求并返回紧凑的结构化识别结果。

## 运行边界

- Main Agent 只传 `page_capture_id`，不传 Skill 文本、DOM、截图或 `task_id`。
- Tool 从 `AgentRunContext` 读取 `task_id`，数据库查询必须同时约束 `task_id` 与 `page_capture_id`。
- DOM 清洗后分块作为文本 Content；截图字节作为真实 `image/png` Content，禁止把 base64 或 artifact ID 当图片内容。
- Skill、页面目录、安全边界和输出 Schema 进入嵌套模型请求的系统指令。
- Tool 的结构化结果是页面识别唯一结论；Main Agent 不二次猜测页面类型。
- `recognized` 且 canonical `page_type` 有效时才允许进入 rule-matching；`ambiguous`/`insufficient` 要求补采；`error` 阻断常规规则匹配。

## 原型取舍

本期按已确认方案将取证读取、DOM 处理、消息构造、模型调用与结果校验集中在 `page_detection_tools.py`，不新增 `PageDetectionService`。该文件是一次性原型边界；后续稳定后再拆分仓储、证据读取和应用服务。

## 安全与审计

- DOM、截图都按不可信页面数据处理，不得覆盖系统指令。
- Tool 返回中不包含原始 DOM、图片字节或 base64，只返回识别结果、证据 ID、Skill/目录版本和模型信息。
- 找不到归属正确的采集、证据不完整、模型错误或目录外页面类型时，结果必须阻断 rule-matching。
