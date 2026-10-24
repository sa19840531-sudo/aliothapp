# Web V2 六站阻断与单详情闭环设计

## 1. 背景与目标

本设计用于完成以下六个桌面 Web 站点的有界探索：

- `https://heiliao.com/`
- `https://hl365.com/`
- `https://www.91jav1.com/`
- `http://91porna.com/`
- `http://huangguoai.com/`
- `https://avhub360.com/`

平台必须打开首页，处理首页及后续菜单/详情页上的普通阻断，按独立 Web 菜单 Catalog 顺序打开每站最多 10 个同源菜单，并为每个菜单只采集一个逻辑详情。禁止扩展到 App/H5 探索链路。

六站按当前 Catalog 需要执行 54 个同源菜单：heiliao 10 个、hl365 10 个、91jav1 6 个、91porna 10 个、huangguoai 8 个、avhub360 10 个。列表菜单只能选择一个详情目标；菜单本身已经是详情页时，直接把菜单 ready Capture 作为该菜单唯一详情。

## 2. 已确认根因

### 2.1 绝对 z-index 阈值会同时漏报和误报

- 91jav1 的真实弹窗为 `#popup-window`，关闭包装节点是 `.close-img.on-popup-close`，有效 z-index 为 2000，但节点没有关闭文字，当前候选能力被降为普通交互，最终不能执行。
- avhub360 的真实 `role="dialog"` 只有 z-index 100，关闭图片具有 `alt="关闭"`；固定页头却有 z-index 1900。以 `z-index > 500` 生成阻断候选会漏掉真实 Dialog 并选中页头。
- 91porna 菜单页存在 z-index 1050 的公告弹窗和“立即关闭”按钮，说明阻断识别还必须排除弹窗内部的地址链接，不能把它们当作详情候选。

结论：普通 Web 阻断不再使用绝对 z-index 门槛。截图负责判断是否形成视觉阻断，DOM 负责证明阻断容器与安全关闭控件。

### 2.2 阻断动作身份超过数据库物理长度

当前关闭动作的 `intent_id` 使用 `web-readiness:<capture-id>:<candidate-id>`。候选 ID 包含 `web-candidate:` 和 64 位哈希，完整 ID 达到 129 字符，而 `qc_action_execution.intent_id` 为 `varchar(128)`。91porna 的 10 个菜单都在插入关闭动作时触发 `StringDataRightTruncationError`。

### 2.3 详情候选错误依赖首屏命中

- heiliao 的真实 `/archives/<id>/` 链接已存在于菜单 DOM，具有重复 `video-item` 结构，但因为悬浮区域覆盖，中心命中为 false，当前提取器返回空候选。
- 91jav1 和 avhub360 的 `/videos/<slug>`、`/article/<id>`、`/article/index/id/<id>` 已存在于 DOM，弹窗存在时同样无法通过中心命中。
- huangguoai 的 `/detail/<id>/` 已存在于重复 `hg-drama-card` 中，但页面分类结构信号不足，流程在详情提取前收口。
- hl365 已存在 `/archives/<id>.html` 主卡片链接，说明不需要站点专用 selector 才能识别详情。

结论：详情目标应从 ready HTML/DOM 的可信同源锚点中提取并直接导航，不应要求链接当前位于首屏或可被鼠标中心点击。

### 2.4 运行时收口和账本边界不完整

- hl365 关闭时遗留一条 `evidence_pending` 动作，导致 Surface Session 无法关闭，并使已经完成的菜单摘要被空 fallback 报告替代。
- 运行时发现的导航被追加到 `menu_ledger`，使权威 Catalog 账本长度从 30 变成 76、从 65 变成 120。
- 捕获异常时把 Python 异常类名拼到 `error_code`，破坏固定错误码合同。

## 3. 总体架构

采用“确定性结构证明 + LLM 只读选择 + 可信执行器验签”的混合方式：

1. 浏览器 Capture 保存 1920×1080 截图、DOM、UI Snapshot 和动作轨迹。
2. 可信代码从 UI Snapshot 生成阻断层和关闭候选，LLM 只判断截图中的视觉阻断并返回候选 ID。
3. 关闭后重新 Capture，验证原阻断签名消失；每层都保留前后证据。
4. 页面无遮挡后，可信代码从 HTML 派生的锚点事实中生成详情候选；LLM 只返回一个候选 ID。
5. 代码解析候选绑定的同源 URL，以 Navigate 动作打开一次详情，不依赖页面点击。
6. Catalog 菜单账本与运行时发现账本分开，任何运行时新增链接都只记录、不执行。
7. Web V2 会话关闭前只清理本 Web Session 遗留的 `evidence_pending` 动作；App/H5 不进入该清理分支。

## 4. 普通阻断合同

### 4.1 阻断层证明

可信 Snapshot 为可见节点计算以下结构事实：

- 是否位于 `dialog/modal/popup/overlay` 或悬浮广告容器；
- `role="dialog"`、`aria-modal`、`data-open` 等显式状态；
- 是否覆盖视口中心、主体内容或大面积交互区域；
- 是否造成主体链接命中失败或页面滚动锁定；
- 是否属于 `header/nav/sidebar` 或其他结构导航。

阻断层候选必须满足：容器当前可见，并且截图判定其阻断页面主要浏览或交互。固定页头、菜单、侧栏和不影响主内容的小型浮层不得成为普通阻断。

### 4.2 安全关闭候选

候选必须属于已经证明的当前阻断层，且至少满足一项关闭语义：

- 文本、`aria-label`、`title` 或图片 `alt` 包含关闭、取消、稍后、知道了、跳过、close、dismiss 或叉号；
- class/id 包含受限 `close/dismiss` token；
- 阻断层内存在委托关闭包装结构，例如 `on-popup-close`、`on-dialog-close`；
- 父包装层包含唯一关闭图片或图标。

对 `div/img` 这类非原生按钮，只允许在阻断层已经证明、关闭语义唯一、节点可见且命中测试通过时签发 `web_obstruction_dismiss`。普通链接、导航、弹窗内地址按钮和正文按钮不得提升为关闭能力。

### 4.3 LLM 与执行边界

- LLM 输入仅包含截图、DOM 证据引用和不含 selector 的候选公开投影。
- LLM 返回 `obstruction_present`、`dismiss_candidate_id`、置信度和理由。
- 代码复核候选绑定当前 Capture、属于当前阻断层且能力为 `web_obstruction_dismiss`。
- 点击后必须重新采集并验证原阻断层签名消失或可见状态发生可信变化。
- 每页最多关闭 12 层，总预算 120 秒。
- 人机验证只阻断，不点击；18 岁门禁继续由独立年龄流程处理。

## 5. HTML 到唯一详情合同

### 5.1 候选来源

详情候选来自 menu ready Capture 中由 HTML/DOM 生成的锚点事实。候选不要求位于首屏，也不要求 `center_hits_target=true`，但节点必须在 DOM 中真实渲染、具有 HTTP(S) href，并且能绑定来源 Capture。

### 5.2 安全过滤

候选必须：

- 与站点同源，允许 HTTP 到同主机 HTTPS 的安全升级；
- 不含用户名、密码和非 HTTP(S) scheme；
- 不是当前菜单 URL、fragment-only URL 或空首页别名；
- 不位于 Web 导航、分页、面包屑、广告、弹窗、登录注册、下载支付、页脚法律区；
- 不属于分类标签、元数据、作者、专题筛选或“更多”链接；
- 位于重复内容卡片、`article`、卡片标题/封面或具有同形 URL 模板的主体链接中。

同一卡片的封面和标题指向同一规范 URL 时只保留一个候选。一个卡片中的 tag/category 链接不能覆盖标题/封面主链接。

### 5.3 选择与导航

- 可信侧为候选生成稳定 ID，并保存规范 URL、来源 Capture、DOM 结构证据和预期详情类型。
- LLM 只看到候选 ID、文字摘要、矩形和结构证据，不看到 selector 或可编辑 URL。
- LLM 必须返回一个当前闭集中的候选 ID，置信度不低于 0.8。
- 执行器从可信候选恢复 URL，使用低风险 Navigate 动作打开，不进行第二次候选选择。
- 详情完成清障和页型复验后立即结束当前菜单。
- 每个菜单的 `detail_action_count` 必须小于等于 1。

## 6. 五类页面分类

继续使用独立 `web_page_type_catalog.json`，只保留：

- `home`
- `web_video_list`
- `web_video_detail`
- `web_content_list`
- `web_content_detail`

新增的结构事实包括：主详情链接数、重复主卡片数、媒体卡片数、播放器数和单一主体数。视频列表可以由重复封面/标题卡片、`video-img-box/poster/thumbnail/drama-card` 等证明，不要求列表页本身存在 `<video>`。内容列表必须有重复主链接。详情页必须有播放器或单一主体证明。

菜单本身已经被证明为 `web_video_detail` 或 `web_content_detail` 时，直接将菜单 ready Capture 记为唯一详情。静态“回家的路”页面若具有单一主体，则属于 `web_content_detail`，不得新增第六种页型。

## 7. 运行时和持久化

### 7.1 固定长度动作身份

关闭动作使用固定长度哈希身份，例如 `web-ready:<sha256>`。原 Capture ID 和候选 ID继续保存在 ActionIntent 参数中供验签，禁止继续把多个协议 ID直接拼入 `intent_id`。

### 7.2 双账本

- `menu_ledger`：只包含 Catalog 中的权威菜单，长度始终等于本站 Catalog 条目数。
- `runtime_discovered_ledger`：保存运行时新增导航，只记录、不执行，不参与菜单完成率和限制计数。

### 7.3 错误合同

`error_code` 只允许固定 `WEB_[A-Z0-9_]+`。异常类型和诊断摘要写入独立诊断字段，不得拼接到错误码。

### 7.4 Web-only 会话清理

在 Web V2 Session 进入 draining 后、关闭 readiness 检查前，清理器只处理以下记录：

- Session owner 为 `web-v2-workflow`；
- Session ID 以 `web:` 开头；
- Action 状态为 `evidence_pending`；
- Session 已释放租约并处于 draining。

这些记录保守终结为失败并写入固定诊断，防止 Driver 因历史遗留动作无法关闭。H5/App Runtime 不注入该清理器。

即使 Driver 关闭仍失败，执行器也必须在失败 RunResult 中保留已经形成的 `web_exploration_summary`、`page_timeline` 和菜单账本。

## 8. 截图和证据

- 浏览器视口、screen 和截图尺寸固定为 1920×1080。
- PNG 只允许无损体积压缩，不允许修改宽高。
- 首页、菜单 probe、阻断关闭前后、菜单 ready、详情 probe、详情 ready 均保留证据关联。
- 每个菜单保存全部详情候选 ID、唯一选中候选 ID、置信度、理由、来源 Capture、详情 Capture 和动作次数。

## 9. 验收标准

- 六站共 54 个同源菜单全部形成终态。
- 54 个菜单各有一个逻辑详情结果；直接详情菜单使用自身 ready Capture。
- 每个菜单最多一次逻辑详情导航，绝不递归采集第二条详情。
- 91jav1 和 avhub360 首页阻断均可被安全关闭并验证消失。
- `menu_ledger` 长度与 Catalog 一致，`runtime_discovered_ledger` 独立。
- 六站 `frontier_remaining=0`，没有非法错误码和数据库截断。
- 所有留存 PNG 均为 1920×1080。
- Web 专项测试、App/H5 边界测试和 Ruff 全部通过。
