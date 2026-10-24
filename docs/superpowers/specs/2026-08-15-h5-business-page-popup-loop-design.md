# H5 业务页弹窗循环与任务时限设计

## 1. 目标

H5 完整探索任务的总执行时长固定为 3600 秒，单次浏览器动作不再设置独立的 30 秒上限。除 `line_loading` 和 `startup_ad` 外，每个业务页面都必须持续处理当前最上层弹窗：每轮关闭后重新采集、重新识别，直到弹窗消失；同一业务页面最多执行 20 轮。

本设计只改变 H5 探索控制与 Web Interaction 超时边界，不改变规则判定、数据库表结构、前端菜单或路由。

## 2. 已确认口径

- 任务总时长：3600 秒。
- 单动作时长：没有独立固定上限，只能使用任务 deadline 的剩余时长。
- 业务页面身份：`规范化 URL + page_type`。
- 同一业务页面因弹窗关闭产生的重复 Capture 共用同一份 20 轮额度。
- 不同规范化 URL 或不同 `page_type` 分别计数。
- `line_loading`、`startup_ad` 不进入业务页弹窗计数，也不消耗 20 轮额度。
- 第 20 轮关闭后若重新识别仍为弹窗态，不再发起第 21 次关闭动作；任务以可审计的有界未覆盖状态收口。

## 3. 方案选择

采用 H5 Frontier 计数方案，不把 H5 生产探索接入通用 `popup_chain` 阶段，也不在浏览器驱动内部一次性循环点击。

原因如下：

1. H5 Frontier 已保存每个弹窗关闭候选的来源 URL、候选元数据、遮挡指纹和处理状态，可直接形成可恢复计数事实。
2. 每轮仍经过 `before Capture → Action → after Capture → 页面识别 → Frontier`，不会丢失分层弹窗的证据、向量导航审计和动作账本。
3. 浏览器驱动只负责执行一个已经验证的关闭动作，不承担页面级策略和循环计数，领域边界保持清晰。
4. 不需要数据库迁移，也不需要引入第二套 H5 阶段状态机。

## 4. 页面身份规范化

新增纯函数生成业务页面键：

```text
business_page_key = normalized_location + "|" + normalized_page_type
```

URL 规范化规则：

- scheme 与 hostname 转为小写；
- 移除 HTTP 80、HTTPS 443 默认端口；
- 空 path 规范为 `/`；
- 保留 path、query 和 fragment，因为 H5 SPA 可能使用 query 或 hash 表达真实业务路由；
- 不接受空地址、`about:blank`、非 HTTP(S) 地址作为业务页面键。

`page_type` 去除首尾空白并转为小写。类型为空、`line_loading` 或 `startup_ad` 时不生成业务页面键。

## 5. 弹窗轮次计数

H5 Frontier 条目是计数的唯一事实来源，不新增独立缓存或进程内计数器。

一个条目计入某业务页面的弹窗处理轮次，必须同时满足：

- 候选为 `click + low risk + navigation_depth=0`；
- `semantic_target == "关闭当前弹窗"`；
- 候选包含受信 `popup_dismissal`、`close_control` 和合法 `obstruction_signature`；
- 条目的 `source_location` 规范化后等于当前业务页 URL；
- 候选元数据中由页面识别边界固化的 `source_page_type` 等于当前业务页 `page_type`；`expected_next_page_type` 只描述动作后的预期页面，不能用于来源页面归属；
- 条目已经进入 `executed`，或者进入带有 `selected_at` 与 `finished_at` 的 `failed` / `blocked` 终态。当前 `selected` 条目尚未完成本轮，不提前计数，未真正选择过的策略阻断条目也不计数。

同一遮挡指纹继续按现有 Frontier 规则去重，避免页面波动导致同一弹窗重复消耗轮次。不同遮挡指纹代表下一层或后续弹窗，各消耗一轮。

## 6. 探索数据流

1. 页面识别返回业务页类型、弹窗状态和受信关闭候选。
2. H5 Frontier 合并候选并选择当前最上层弹窗关闭动作。
3. H5 Completion/Selection 边界根据当前 `business_page_key` 统计已完成的弹窗处理轮次。
4. 已完成轮次少于 20 时，允许当前关闭动作进入 Web Interaction Runtime。
5. Runtime 解析当前 UI Snapshot 中的唯一真实关闭控件并执行点击；离屏控件仍可使用已验证、可审计且必须经过关闭后验证的受限兜底。
6. 动作完成后重新采集并重新执行页面识别，不继承旧弹窗结论。
7. 若新 Capture 不再是弹窗态，业务页进入正常 Frontier 探索。
8. 若仍有不同弹窗，则生成下一条受信关闭候选，回到步骤 2。
9. 已完成 20 轮且页面仍为弹窗态时，不生成或不执行第 21 次关闭动作，返回 `popup_layer_limit_exceeded`。

## 7. 时限与错误处理

- H5 任务启动时的 deadline 固定为开始时间加 3600 秒。
- `BrowserInteractionExecutor` 删除 30 秒截断，不再使用 `min(remaining, 30)`。
- 浏览器请求与组合 Capture 使用 `remaining_task_seconds`，确保动作可以超过 30 秒，但不能越过任务总 deadline。
- 动作开始前 deadline 已到时返回 `ACTION_DEADLINE_EXCEEDED`。
- 动作执行到任务 deadline 时仍可返回 `ACTION_TIMED_OUT`，其含义是任务剩余时长耗尽，不再代表 30 秒单动作限制。
- 第 20 轮后仍有弹窗时返回 `popup_layer_limit_exceeded`，任务按有界未完整覆盖收口，不能伪报 completed。
- 关闭控件不存在、不唯一或关闭后原 Dialog 未消失时，继续使用现有结构化错误码，不通过猜测坐标、通用 selector 或无限重试绕过安全门禁。

## 8. 审计与可观测性

最终任务必须能够从现有事实表重建每个业务页的弹窗处理过程：

- `qc_action_execution`：动作序号、状态、开始/结束时间、错误码；
- `qc_page_capture`：每轮 before/after Capture、URL、page_type、page_state；
- `qc_h5_page_type_assessment`：页面重新识别、候选动作和导航知识审计；
- H5 Frontier 快照：来源 URL、来源页面类型、预期页面类型、遮挡指纹和条目状态。

任务结果或日志至少记录业务页面键、已完成弹窗轮次和上限原因，但不得记录敏感凭证、授权头或 LLM API Key。

## 9. 测试设计

### 9.1 单动作无固定上限

- 使用接近 3600 秒的 ActionIntent deadline；断言 `PageCaptureRequest.timeout_ms` 使用任务剩余时长且明显大于 30000。
- 使用短于 30 秒的剩余 deadline；断言仍使用更短的任务剩余时长。
- deadline 已到时断言 `ACTION_DEADLINE_EXCEEDED`。

### 9.2 每业务页最多 20 轮

- 同一 URL、同一业务页类型已有 19 条终态弹窗关闭条目时，第 20 次允许执行。
- 已有 20 条终态弹窗关闭条目且仍为弹窗态时，阻止第 21 次并返回 `popup_layer_limit_exceeded`。
- 同 URL 不同 `page_type` 独立计数。
- 同 `page_type` 不同规范化 URL 独立计数。
- URL 仅在 scheme/host 大小写、默认端口或空 path 上不同时归并为同一页面。
- query 或 fragment 不同视为不同 SPA 业务路由。
- `line_loading`、`startup_ad` 不进入计数。
- 普通业务点击、导航、滚动和媒体动作不消耗弹窗轮次。

### 9.3 邻近回归与真实验收

- 运行 H5 Frontier、H5 Turn、Web Capture Runtime、页面驱动和弹窗关闭相关测试。
- 无 build 重启 API/worker，确认镜像 ID 不变。
- 自动登录并提交全新任务，观察每轮弹窗关闭、页面重新识别和 Frontier 继续推进。
- 最终列出真实访问页面、13 类目标页面覆盖情况、未覆盖原因、向量查询尝试/命中以及 worker 终态。

### 9.4 H5 V2 固定移动端视口

- H5 V2 与现有 H5 V1 一致，所有探索 Capture 的实际视口固定为 `390×844`。
- 首次 Surface Session、首次导航、普通 Frontier 候选、主动登录导航、来源页恢复和浏览器会话丢失后的新 Session/恢复导航必须使用同一视口参数。
- 固定值由 H5 应用层单一策略提供；模型候选不能覆盖，非 H5 Web 链路不受影响。
- 回归必须验证 Surface Session 审计参数和实际 `ActionIntent.parameters`，真实验收再以 PNG 尺寸或可信 UI Snapshot 复核浏览器输出。

## 10. 变更范围

预计修改：

- `services/api/app/infrastructure/browser/capture_runtime.py`
- `services/api/tests/test_web_capture_runtime.py`
- H5 Frontier 或 H5 Completion/Selection 边界的一个专用模块及其测试
- `services/api/app/application/h5_exploration_observation.py` 中可信弹窗候选的来源页面类型元数据
- `services/api/app/application/h5_viewport.py` 及 H5 启动、认证、普通动作和恢复链路
- `docs/专项能力与高频回归规则.md`

不涉及：

- 数据库迁移；
- 前端菜单或路由；
- 规则集结构；
- Docker 镜像构建。
