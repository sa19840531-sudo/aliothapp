# page_capture_tool 设计说明

> 日期：2026-07-09
> 范围：本轮只实现“直接返回 + 预留 artifact_id 字段”，不接对象存储、不落证据库。

## 目标

`page_capture_tool` 是 Agent 可调用的统一页面采集入口。它不是 Playwright 本身，也不直接承担浏览器工程细节；Playwright 作为独立浏览器处理层，封装在 `services/api/app/infrastructure/browser` 下。

本轮目标是跑通页面采集最小闭环：导航、只读观测、多帧定时采集、单次最小触发，并返回结构化观测结果。

## 分层

| 层级 | 目录 | 职责 |
| --- | --- | --- |
| Tool 契约层 | `services/api/app/agents/tools/page_capture_tools.py` | 注册 `page_capture_tool`，校验模式与危险动作，调用浏览器采集服务，返回结构化结果 |
| 浏览器处理层 | `services/api/app/infrastructure/browser/page_capture.py` | 封装 Playwright 的导航、截图、DOM、可见文本、console/network 采集和触发动作 |
| 未来证据存储层 | `services/api/app/infrastructure/storage` | 后续保存截图、HAR、DOM 等产物，并回填 `artifact_id` |

## 四种模式

- `goto`：打开或跳转 URL，返回可达性、最终 URL、标题和状态码，并附带基础观测。
- `observe`：只读采集当前或指定 URL 的 DOM、可见文本、截图、控制台日志和网络请求。
- `timed_capture`：按间隔连续采集截图或读值，用于倒计时、闪烁、秒开、卡顿、连续弹窗、续播等时序证据。
- `single_trigger`：只允许一次 `click`、`scroll`、`hover` 或 `network_fault`，触发后立即返回结果态快照。

## 安全边界

本 Tool 只做“采集 + 单次触发观测”。默认拒绝高风险动作，包括登录、支付、提交表单、上传、下单、完整流程执行、造数据。

V1 将整个 `page_capture_tool` 标记为 `SENSITIVE`，因为即使 `goto` 和 `observe` 本身偏只读，统一入口包含 `single_trigger` 能力，必须经过权限中间件和任务规划边界放行。

## 输出契约

返回结构保持稳定：

- `execution_status`：`success` / `blocked` / `error`
- `mode`：`goto` / `observe` / `timed_capture` / `single_trigger`
- `trigger_action`：仅在 `single_trigger` 使用，只填写 `click` / `scroll` / `hover` / `network_fault` 动作名。
- `selector`：`click` / `hover` 的独立目标选择器；例如关闭按钮必须使用 `trigger_action="click"`、`selector="text=关闭"`，不得把动作和目标拼入同一字段。
- `reachable`、`final_url`、`title`、`status_code`
- `artifacts`：列表，每项包含 `artifact_type`、`artifact_id`、`content`
- `console_logs`
- `network_requests`
- `action_trace`
- `post_action_snapshot`

`artifact_id` 本轮固定预留为 `null`，后续接入对象存储后再填真实 ID。

## 不做范围

- 不接对象存储。
- 不新增数据库迁移。
- 不实现完整页面流程自动化。
- 不允许填表、提交、上传、支付、登录。
- 不把 Playwright API 暴露给 Agent。
