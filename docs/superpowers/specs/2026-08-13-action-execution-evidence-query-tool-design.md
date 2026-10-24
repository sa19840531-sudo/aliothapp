# Action Execution 证据查询 Tool 设计

## 1. 背景与目标

Alioth 已通过 `qc_action_execution`、`qc_capture`、`qc_page_capture` 与
`qc_evidence_artifact` 持久化 Web/H5 动作及其前后证据，但当前 Agent
缺少一个能够按 `action_execution_id` 等待并直接取得完整动作证据包的只读 Tool。

本设计新增 `get_action_execution_evidence_tool`。Agent 只传动作执行 ID，Tool
从当前 `AgentRunContext` 绑定任务身份，等待动作完成及前后证据齐备，然后返回
版本化的 `action-evidence-bundle.v1` 结构。

## 2. 已确认范围

### 2.1 范围内

- 新增只读 Agent Framework Function Tool。
- 输入 `action_execution_id` 与可选等待秒数。
- 默认等待 60 秒，最大等待 180 秒。
- 等待 Action Execution 完成，并要求 before/after 均具备页面类型、DOM、截图和
  UI Snapshot。
- 返回动作、前后页面、Artifact 元数据及状态差异。
- 超时、失败终态及查询异常均返回结构化结果。
- 新增单元测试。

### 2.2 范围约束

生产代码改动仅允许位于 `services/api/app/agents/tools`：

- 新增 `action_evidence_tools.py`。
- 修改 `tools/__init__.py`，导入新模块并触发 Tool 注册。

允许新增测试文件。不得修改领域层、应用层、仓储层、Agent Bootstrap、数据库模型
或迁移。

### 2.3 范围外

- 不执行或重放浏览器动作。
- 不按 `task_id`、Capture ID 或 URL 提供任意查询。
- 不读取或返回 DOM、截图、UI Snapshot 的正文或二进制内容。
- 不为旧动作补采缺失证据。
- 不新增 REST API 或前端页面、菜单和路由。
- V1 不扩展为 App/Android/iOS Capture 查询。

## 3. Tool 契约

### 3.1 名称与权限

```python
get_action_execution_evidence_tool(
    action_execution_id: str,
    timeout_seconds: int = 60,
) -> dict[str, Any]
```

- Tool 分类：`动作证据查询`。
- 权限级别：`PermissionLevel.SAFE`。
- `action_execution_id` 必须是非空字符串。
- `timeout_seconds` 小于 1 时按无效请求处理；大于 180 时收敛为 180。
- Tool 不接受 `task_id`。任务身份只能来自 `require_agent_run_context()`。

### 3.2 成功输出

```json
{
  "protocol_version": "action-evidence-bundle.v1",
  "action_execution_id": "action-001",
  "status": "completed",
  "wait": {
    "outcome": "ready",
    "timeout_seconds": 60,
    "elapsed_ms": 2150,
    "missing_requirements": []
  },
  "before": {
    "capture_id": "uuid-before",
    "url": "https://example.com/list",
    "page_type": "product_list",
    "captured_at": "2026-08-13T10:00:00Z",
    "dom": {
      "artifact_id": "uuid",
      "storage_path": "..."
    },
    "screenshot": {
      "artifact_id": "uuid",
      "storage_path": "..."
    },
    "ui_snapshot": {
      "artifact_id": "uuid",
      "storage_path": "..."
    }
  },
  "action": {
    "type": "click",
    "target": "商品详情",
    "selector": "a.product-detail",
    "purpose": "进入商品详情页",
    "before_url": "https://example.com/list",
    "after_url": "https://example.com/detail/1001",
    "transition_kind": "business_navigation",
    "new_page_urls": []
  },
  "after": {
    "capture_id": "uuid-after",
    "url": "https://example.com/detail/1001",
    "page_type": "product_detail",
    "captured_at": "2026-08-13T10:00:02Z",
    "dom": {
      "artifact_id": "uuid",
      "storage_path": "..."
    },
    "screenshot": {
      "artifact_id": "uuid",
      "storage_path": "..."
    },
    "ui_snapshot": {
      "artifact_id": "uuid",
      "storage_path": "..."
    }
  },
  "diff": {
    "url_changed": true,
    "state_diff": {},
    "target_before": {},
    "target_after": {}
  }
}
```

Artifact 对象仅公开 `artifact_id` 与 `storage_path`。V1 不返回正文、校验和、内部
数据库行或对象存储凭据。

## 4. 数据来源与字段映射

查询必须以 `qc_action_execution` 为主表，并使用
`action_execution_id + AgentRunContext.task_id` 共同限定。不得从完整 Capture
内连接反推 Action Execution，避免证据缺失时整条动作从结果中消失。

| 输出字段 | 数据来源 |
| --- | --- |
| `status` | `qc_action_execution.status` |
| `action.type` | `intent_payload.action_type` |
| `action.purpose` | `intent_payload.purpose` |
| before ID | `qc_action_execution.source_capture_id` |
| after ID | `qc_action_execution.result_capture_id` |
| URL、页面类型 | 对应 `qc_page_capture` |
| 采集时间 | 对应 `qc_capture.captured_at` |
| Artifact | 对应 `qc_evidence_artifact` |
| selector、导航、目标事实、状态差异 | after `qc_page_capture.action_log` 的最后一条相关动作 |

`action.target` 按以下顺序选择：

1. `target_before.text`；
2. `target_before.aria_label`；
3. `target_after.text`；
4. `target_after.aria_label`；
5. `intent_payload.semantic_target`；
6. `null`。

`action.selector` 仅来自 Action Trace。现有 Action Intent 有意不持久化平台选择器，
Tool 不得从语义目标推造 selector。

`action.before_url`、`action.after_url` 优先取 Action Trace；缺失时回退到 before/after
Capture URL。`diff.url_changed` 由最终输出中的两个 URL 比较得到。

同一 Capture 存在多个同类型 Artifact 时，按 `created_at ASC, id ASC` 选择首个作为
协议主 Artifact，保证返回稳定。其余同类型 Artifact 在 V1 中不展开。

## 5. 归属与完整性校验

before/after ID 必须以 Action Execution 的 `source_capture_id` 与
`result_capture_id` 为权威，禁止按角色模糊选择。完整结果必须同时满足：

- Action Execution 属于当前任务且状态为 `completed`。
- before 与 after 均存在于 `qc_capture` 和 `qc_page_capture`。
- 两侧 `task_id` 均与当前任务一致。
- 两侧 `surface_kind` 均为 `web`。
- 两侧 `action_execution_id` 均与请求一致。
- before/after 的 `capture_role` 分别为 `before_action`、`after_action`。
- 两侧 `capture_status` 均为 `captured`。
- 两侧 URL 与 `page_type` 均为非空。
- 两侧均存在 `dom`、`screenshot`、`ui_snapshot` Artifact。
- 每个 Artifact 的 `task_id` 与 `page_capture_id` 均与其 Capture 一致。

缺失项使用稳定路径表示，例如：

```json
[
  "before.dom",
  "before.ui_snapshot",
  "after.page_type"
]
```

归属、角色或动作 ID 不一致属于完整性缺口，不得当作可用证据返回。相关路径分别使用
`before.identity` 或 `after.identity`。

## 6. 等待状态机

Tool 使用单调时钟计算耗时，每 500ms 重新查询一次：

```text
按 action_execution_id + 当前 task_id 查询
  ├─ 查不到：继续等待
  ├─ planned / executing / evidence_pending：继续等待
  ├─ completed 但证据不完整：继续等待
  ├─ completed 且证据完整：ready
  ├─ blocked / failed / timed_out / evidence_failed：terminal_failure
  └─ 达到 timeout_seconds：timeout
```

查询不到时不得再按未限定任务的 Action ID 查询，因为这会泄露其他任务是否存在该
动作。直到超时时都使用 `action_execution` 作为缺失项。

失败终态已经不可转为成功，Tool 立即返回 `terminal_failure`，不无意义等待到超时。
返回中保留已经存在且通过归属校验的数据，同时列出剩余缺口。

动作 `completed` 但页面识别或 Artifact 尚未齐全时继续等待，直至满足完整条件或超时。

## 7. 结构化异常

`wait.outcome` 的稳定枚举为：

- `ready`：动作完成且完整证据已齐备。
- `timeout`：达到等待上限。
- `terminal_failure`：动作进入失败、阻断、动作超时或证据失败终态。
- `blocked`：缺少业务 Agent Run 上下文，禁止查询。
- `invalid_request`：动作 ID 或等待参数无效。
- `error`：数据库查询异常。

非 `ready` 结果仍保留顶层协议结构；可用数据正常返回，缺失的 `before`、`after`、
`action` 或其子字段使用 `null`。`wait` 在非 ready 结果中增加：

```json
{
  "error_code": "ACTION_EVIDENCE_QUERY_FAILED",
  "error_message": "动作证据查询失败"
}
```

稳定错误码包括：

- `ACTION_EVIDENCE_CONTEXT_REQUIRED`
- `ACTION_EVIDENCE_INVALID_REQUEST`
- `ACTION_EVIDENCE_TERMINAL_FAILURE`
- `ACTION_EVIDENCE_WAIT_TIMEOUT`
- `ACTION_EVIDENCE_QUERY_FAILED`

数据库异常不得向 Agent 暴露连接串、SQL、表结构或驱动原始错误。

## 8. 单文件实现边界

由于生产代码只允许新增 Tool，`action_evidence_tools.py` 内部包含以下私有职责：

- Tool 装饰与公开入口。
- 请求规范化。
- PostgreSQL 只读查询。
- 查询行到证据快照的组装。
- 完整性缺口计算。
- Action Trace 字段映射。
- 轮询与结构化错误处理。

这些职责必须拆成小型私有函数或私有不可变数据结构，禁止把 SQL、轮询、映射和 Tool
入口堆在一个长函数中。测试通过替换私有单次查询函数及等待函数控制状态序列，不向
Agent 的 JSON Schema 暴露测试注入参数。

`tools/__init__.py` 只负责导入并导出 `action_evidence_tools`，不得加入业务逻辑。

## 9. 测试设计

新增 `services/api/tests/test_action_evidence_tools.py`，至少覆盖：

1. Tool 注册名、分类与 `SAFE` 权限。
2. 绑定当前 `task_id`，Tool 参数中不存在 `task_id`。
3. 完整数据首次查询即返回 `ready`。
4. 初次证据不全、后续补齐后返回 `ready`。
5. `completed` 但缺少任一页面类型或必需 Artifact 时继续等待。
6. 默认等待 60 秒，超过 180 秒被收敛为 180 秒。
7. 超时返回当前状态及排序稳定的 `missing_requirements`。
8. Action Execution 不存在时不执行跨任务探测。
9. `blocked`、`failed`、`timed_out`、`evidence_failed` 立即返回
   `terminal_failure`。
10. Action Trace 正确映射到 `action` 与 `diff`。
11. Trace 缺字段时正确回退到 Capture 和 Intent。
12. 多个同类型 Artifact 的选择稳定。
13. Capture/Artifact 归属或角色不一致时报告 identity 缺口。
14. 缺少 Agent Run 上下文时不访问数据库。
15. 查询异常返回通用错误，不泄露原始异常。
16. 导入 `tools` 根包后，默认注册表中存在该 Tool。

SQL 合同测试必须断言查询同时包含动作 ID 和任务 ID 条件，并以 Action Execution 为
驱动表保留不完整动作。

## 10. 文件与迁移影响

- 新增：`services/api/app/agents/tools/action_evidence_tools.py`
- 修改：`services/api/app/agents/tools/__init__.py`
- 新增：`services/api/tests/test_action_evidence_tools.py`
- 数据库迁移：无
- 前端菜单/路由同步：无
- 文档：本设计文档；实现完成后按仓库规则评估是否需要补充
  `docs/专项能力与高频回归规则.md`

## 11. 验收标准

- Agent 在绑定业务任务上下文后，可仅凭 `action_execution_id` 调用 Tool。
- Tool 不执行浏览器动作、不接受模型传入的任务身份。
- 完整动作在 60 秒默认窗口内返回 `ready` 及完整协议。
- `ready` 必须同时满足动作完成与两侧页面识别/三类 Artifact 完整。
- 最长等待不超过 180 秒。
- 超时和失败均为结构化返回，不产生未处理异常。
- 跨任务动作 ID 不泄露存在性或任何证据元数据。
- 生产代码改动严格限制在 `agents/tools` 目录。
- 全部新增测试通过，现有 Tool 注册与权限测试不回归。
