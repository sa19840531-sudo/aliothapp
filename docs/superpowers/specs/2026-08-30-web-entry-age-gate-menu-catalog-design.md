# Web 入口年龄门禁、真实首页与全菜单 Catalog 设计

> 2026-08-31 范围修订：单站按 Catalog 固定顺序最多执行前 10 个同源菜单，首页、跨域阻断和所选菜单的一层详情不占菜单额度；其余同源菜单必须以 `menu_limit_skipped` 进入完整账本。本文后续“全部菜单／全菜单”表述均以“全部 Catalog 身份闭账、仅前 10 个同源菜单实际采集”为准。本修订不改变 H5/App 的采集流程、预算或目录。

> 日期：2026-08-30  
> 状态：设计已确认，六站菜单白名单已补充，待实施
> 适用范围：Web V2 / `desktop_fhd_1920x1080_v1`  
> 约束：不得改变 H5/App 的默认目录、清障、认证和探索行为

## 1. 背景

Web V2 打开目标网址后，首个可见页面可能是年龄确认门禁，而不是业务首页。如果
把该门禁截图直接登记为 `home`，会污染页面目录样本、菜单发现、页面覆盖和后续
质检证据。

同时，Web 站点的菜单具有站点差异，不能沿用 H5 底部导航和“页面类型已覆盖即
停止”的策略。即使多个菜单最终都属于同一种 Page Type，每个菜单入口仍必须
分别执行并登记终态。

本设计固定以下顺序：

```text
入口加载完成
  -> 首次截图和 DOM
  -> 年龄门禁识别与处理
  -> 普通弹窗清障
  -> 重新截图和 DOM
  -> 建立真实首页 home
  -> 获取并执行全部菜单
  -> 列表页
  -> 详情页
```

## 2. 目标

1. 目标 URL 加载完成后立即采集入口探测证据。
2. 只在操作者已主动声明成年并显式授权时处理年龄确认门禁。
3. LLM 只识别门禁并选择封存候选 ID，不生成选择器、坐标或点击脚本。
4. 门禁消失并完成普通弹窗清障后，重新采集真实首页基线。
5. 只有真实首页基线可以登记为 `home` 并用于菜单发现。
6. Web 页面目录只包含五种页面类型。
7. 以全部菜单进入终态作为网站探索完成条件，不以 Page Type 覆盖作为终止条件。
8. 保留完整的声明、识别、点击、前后 Capture 和菜单覆盖审计链。
9. 不引入人机验证、验证码识别、滑块处理或 Cloudflare 绕过。
10. 首页、菜单页和详情页都必须先完成逐层阻断弹窗清理，再进行页面判定和探索。
11. 六站菜单按站点、版本和规范化路径建立独立白名单，不把菜单文案当作放行依据。

## 3. 非目标

- 不由 LLM 判断操作者的真实年龄。
- 不把成年自我声明记录为平台已验证年龄。
- 不处理 CAPTCHA、“我不是机器人”、图片验证码或滑块。
- 不把年龄门禁定义为第六种 Page Type。
- 不把菜单名称直接定义为 Page Type。
- 不扫描所有正文链接替代菜单结构。
- 不复用 H5 年龄确认结构合同或 H5 页面目录。
- 不修改现有 `page_type_catalog.json` 的默认加载路径和内容。
- 不因发现 `web_content_list` 或 `web_content_detail` 提前停止菜单探索。
- 不把运行时新发现的未知菜单自动提升为可执行白名单。
- 不把内容卡片和动态详情 URL 写入静态菜单白名单。

## 4. 不可变约束

### 4.1 平台隔离

- Web 只通过 `WEB_V2` 路由和 Web 专属 Runtime 执行。
- H5 继续使用现有 `page_type_catalog.json`。
- Web 使用
  `services/api/app/domain/page/web_page_type_catalog.json`。
- Web 站点菜单使用
  `services/api/app/domain/page/web_site_menu_catalog.json`，与页面类型目录分离。
- Web Catalog 缺失、版本非法或摘要不一致时失败关闭，禁止回退 H5 Catalog。
- Web 站点菜单目录缺失或站点不匹配时，不允许回退为“任意同源链接可执行”。
- Web 年龄门禁能力只能从 Web 组合根注入。

### 4.2 浏览器和证据

- Viewport 与 Screen 固定为 `1920 × 1080`。
- 截图只做 PNG 无损体积优化，不改变像素尺寸。
- 首次入口探测 Capture 不自动计入 `home`。
- 发生过任何点击或清障动作后，必须重新 Capture 才能建立首页基线。
- 页面、候选和按钮判定必须绑定同一个 Capture ID 与页面指纹。

### 4.3 操作者声明

操作者年龄资格只能来自主动声明：

```json
{
  "age_gate_policy": "confirm_adult_entry",
  "operator_age_attestation": {
    "declared": true,
    "status": "self_declared",
    "statement": "操作者确认已年满18周岁并有权访问目标网站",
    "declared_by": "<operator-id>",
    "declared_at": "<timestamp>",
    "statement_version": "adult-access-attestation.v1"
  }
}
```

系统必须记录：

```json
{
  "operator_age_verified": false,
  "age_attestation_status": "self_declared"
}
```

缺少有效声明时，即使 LLM 已识别确认按钮，也不得点击。

## 5. 领域对象

### 5.1 WebEntryGateProbe

入口加载完成后的第一份证据：

```json
{
  "capture_role": "web_entry_gate_probe",
  "page_type": null,
  "coverage_eligible": false,
  "artifacts": [
    "screenshot",
    "dom",
    "visible_text",
    "ui_snapshot"
  ]
}
```

它只负责判断入口阻断，不负责菜单发现和页面覆盖。

### 5.2 WebAgeGateCandidate

从当前最顶层阻断层中确定性提取的交互候选：

```json
{
  "candidate_id": "candidate-1",
  "capture_id": "capture-id",
  "role": "button",
  "visible_text": "<text>",
  "accessible_name": "<name>",
  "interactive": true,
  "disabled": false,
  "center_hits_target": true,
  "inside_top_obstruction": true
}
```

候选必须来自封存 UI Snapshot。LLM 不能补造候选。

### 5.3 WebAgeGateDecision

```json
{
  "schema_version": "web-age-gate-decision.v1",
  "decision": "age_gate_present",
  "confidence": 0.96,
  "confirm_candidate_id": "candidate-1",
  "deny_candidate_ids": ["candidate-2"],
  "reason_codes": [
    "adult_age_statement_present",
    "confirm_and_exit_controls_present",
    "candidate_inside_top_obstruction"
  ]
}
```

允许的 `decision`：

- `age_gate_present`
- `age_gate_absent`
- `uncertain`

LLM 不得返回 CSS、XPath、JavaScript、坐标或任意动作序列。

### 5.4 WebHomeBaseline

只有入口阻断已解除后形成：

```json
{
  "capture_role": "web_home_baseline",
  "page_type": "home",
  "coverage_eligible": true,
  "home_ready": true,
  "artifacts": [
    "screenshot",
    "dom",
    "visible_text",
    "ui_snapshot",
    "network"
  ]
}
```

菜单发现唯一允许的前置条件是 `home_ready=true`。

### 5.5 WebMenuCoverageLedger

菜单覆盖账本决定探索是否完成：

```json
{
  "site_origin": "https://example.com",
  "menu_total": 20,
  "menu_terminal": 20,
  "menu_success": 17,
  "menu_blocked": 2,
  "menu_unreachable": 1
}
```

### 5.6 WebMenuEntry

```json
{
  "menu_entry_id": "sha256:<site+menu-path+href+position>",
  "menu_path": ["主导航", "二级菜单"],
  "href": "https://example.com/path",
  "position": 2,
  "allowlist_decision": "allowlisted_observe_only",
  "allowlist_version": "2026-08-30.1",
  "status": "detail_captured",
  "destination_page_type": "web_video_list",
  "detail_page_type": "web_video_detail"
}
```

菜单身份不能使用 Page Type 代替。

### 5.7 WebSiteMenuCatalog

Web 菜单目录是站点级执行边界，不是 Page Type 规则：

```json
{
  "schema_version": "web-menu-allowlist.v1",
  "site_id": "example_com",
  "origin": "https://example.com",
  "version": "<version>",
  "source_digest": "sha256:<digest>",
  "entries": []
}
```

目录必须版本化并保存来源摘要。每个条目至少包含 `menu_entry_id`、菜单层级路径、
原始 `href`、规范化目标和白名单决策。页面类型提示只能辅助排队，不能替代目标
页面的截图与 DOM 分类。

## 6. 入口状态机

```text
NAVIGATING
  -> ENTRY_LOADED
  -> ENTRY_PROBE_CAPTURED
  -> AGE_GATE_PREFILTERED
      -> AGE_GATE_ABSENT
      -> AGE_GATE_LLM_REQUIRED
      -> HUMAN_VERIFICATION_BLOCKED
  -> AGE_GATE_DECIDED
      -> AGE_GATE_UNCERTAIN
      -> AGE_GATE_CONFIRMATION_BLOCKED
      -> AGE_GATE_CONFIRMATION_ALLOWED
  -> AGE_GATE_CONFIRMED
  -> AGE_GATE_REMOVAL_VERIFIED
  -> REMAINING_OBSTRUCTIONS_CLEARED
  -> HOME_BASELINE_CAPTURED
  -> HOME_READY
  -> MENU_EXPLORATION
```

### 6.1 ENTRY_PROBE_CAPTURED

页面加载完成后立即采集：

- `1920 × 1080` viewport screenshot
- DOM
- Visible Text
- UI Snapshot
- 页面 URL、标题摘要和指纹

此时 `page_type=null`、`coverage_eligible=false`。

### 6.2 年龄门禁预检测

确定性预检测只判断是否值得调用 LLM：

- 是否存在顶层弹窗或全屏遮罩；
- 是否包含 `18`、`18+`、成年、成人、`age`、`adult` 等信号；
- 是否存在进入与退出方向相反的交互候选；
- 是否存在图片型或无可访问文本的可点击候选。

无年龄信号时不调用 LLM，继续普通弹窗清障。

### 6.3 LLM 识别

输入：

- 入口探测截图或门禁裁剪图；
- DOM 与 Visible Text 摘要；
- 封存 UI Snapshot 中的候选列表；
- Capture ID、页面指纹和截图摘要。

输出只能引用候选 ID。

`age_gate_present` 只有在置信度不低于 `0.90` 时才能进入候选按钮安全校验；
低于阈值统一按 `uncertain` 处理。

### 6.4 候选按钮安全校验

该步骤不是人机验证。执行器只验证：

1. Capture ID 仍是当前 Capture；
2. 页面指纹没有变化；
3. 候选存在于封存 UI Snapshot；
4. 候选位于当前最顶层年龄门禁；
5. 候选可见、未禁用并且中心 Hit Test 命中自身；
6. 候选语义为“已成年并进入”；
7. 候选不是“未满18岁”“退出”“离开”；
8. 候选不涉及登录、支付、下载、上传或跨域；
9. 确认候选唯一。

失败时返回 `age_gate_needs_review`，不执行点击。

### 6.5 点击后验证

必须同时满足：

- 原门禁不可见；
- URL 仍为同源 HTTP(S)；
- 未打开未知新窗口；
- 页面核心 DOM 可见；
- 页面可以重新 Capture。

否则返回 `age_gate_confirmation_failed`，禁止菜单探索。

### 6.6 真实首页

年龄门禁和普通阻断层清除后重新采集。只有该 Capture 可以：

- 登记为 `home`；
- 计入页面覆盖；
- 作为 Web Catalog 样本；
- 提取导航；
- 建立菜单覆盖账本。

如果首次入口没有年龄门禁、没有普通阻断且页面已稳定，可以将入口探测 Capture
升级为首页基线。只要执行过任何动作，就必须重新采集。

如果普通弹窗位于年龄门禁上方，首次探测可能暂时看不到年龄信号。每次关闭普通
弹窗后都必须重新采集轻量截图与 DOM，并再次执行年龄门禁预检测；只有年龄门禁
和普通阻断都不存在时才能建立首页基线。

### 6.7 每个页面的统一阻断清理

真实首页形成后，每次进入菜单页或详情页都必须重新执行页面入口探测和有界清障：

```text
PAGE_NAVIGATING
  -> PAGE_LOADED
  -> PAGE_ENTRY_PROBE_CAPTURED
  -> OBSTRUCTION_CANDIDATES_BUILT
  -> TOP_OBSTRUCTION_CLASSIFIED
      -> AGE_GATE
      -> SAFE_DISMISSIBLE_POPUP
      -> HUMAN_VERIFICATION
      -> UNKNOWN_BLOCKING_LAYER
      -> NO_OBSTRUCTION
  -> SAFE_CLOSE_EXECUTED
  -> PAGE_REOBSERVED
  -> OBSTRUCTION_CANDIDATES_BUILT
  -> ...
  -> PAGE_READY_BASELINE_CAPTURED
  -> PAGE_CLASSIFICATION
  -> PAGE_EXPLORATION
```

每一层判断都同时使用：

- 当前 `1920 × 1080` 视口截图；
- 当前 DOM；
- UI Snapshot；
- 元素及祖先的 computed style；
- 有效 stacking context 与 effective z-index；
- 元素矩形、可见性、pointer-events 和中心 Hit Test。

`effective_z_index > 500` 是阻断候选门槛，不是单独的关闭依据。候选还必须满足：

1. 元素或其有效遮罩祖先当前可见；
2. `pointer-events` 不为 `none`；
3. 具有 dialog/modal/overlay 语义，或者明显覆盖页面核心区域；
4. 截图证据显示其位于主内容上方；
5. 中心或关键区域 Hit Test 被该层拦截；
6. 不是普通 Header、非阻断 Toast、角标或仅装饰浮层。

DOM 需要为每个候选保存：

```json
{
  "candidate_id": "obstruction-1",
  "raw_z_index": "1000",
  "effective_z_index": 1000,
  "position": "fixed",
  "pointer_events": "auto",
  "visible": true,
  "role": "dialog",
  "viewport_overlap_ratio": 0.72,
  "covers_core_content": true,
  "center_hit_blocks_content": true,
  "obstruction_signature": "sha256:<digest>"
}
```

### 6.8 阻断关闭循环

```text
采集 before 截图和 DOM
  -> 选择当前最顶层阻断层
  -> 若为年龄门禁，进入年龄门禁子流程
  -> 若为普通阻断，提取同一阻断层内部的安全关闭候选
  -> 校验关闭候选可见、唯一、命中自身且不属于业务动作
  -> 点击关闭
  -> 等待动画和延迟弹窗
  -> 重新采集截图、DOM 和阻断签名
  -> 验证旧阻断消失或签名发生有效变化
  -> 继续下一层
  -> 直到不存在阻断候选
```

关闭候选不得是登录、支付、下载、上传、跳转广告、正文卡片或“未满18岁退出”
之外的业务按钮。不得使用未知坐标点击或直接隐藏高层 DOM。

有界参数沿用现有 Web 清障上限：

- 单页面最多关闭 `12` 层；
- 清障链总时长最多 `45,000 ms`；
- 单次点击最多等待 `5,000 ms`；
- 相同阻断签名连续出现时立即停止重复点击。

达到层数、时长或签名无进展上限时：

```json
{
  "page_state": "popup_blocked",
  "remaining_obstruction": true,
  "coverage_eligible": false,
  "exploration_allowed": false
}
```

当前菜单登记失败终态后继续下一个菜单；如果真实首页无法完成清障，则整站不能
进入菜单阶段。

### 6.9 页面就绪基线

只有同时满足以下条件，页面才能进入分类和探索：

- 不存在有效 `z-index > 500` 阻断候选；
- 不存在年龄门禁；
- 不存在人机验证；
- 页面核心 DOM 可读取；
- 页面已完成有界稳定等待；
- 最终截图、DOM 和 UI Snapshot 属于同一页面指纹。

发生过清障动作时必须重新生成 `page_ready_baseline`。没有发生任何动作且入口
探测已满足全部条件时，可以升级已有 Capture，避免重复截图。

## 7. 人机验证边界

不实现：

- CAPTCHA；
- “我不是机器人”；
- 图片验证码；
- 滑块；
- Cloudflare 验证绕过。

检测到此类阻断时：

```json
{
  "entry_state": "human_verification_blocked",
  "home_ready": false,
  "menu_exploration_allowed": false
}
```

## 8. Web 页面目录

文件位置固定为：

```text
services/api/app/domain/page/web_page_type_catalog.json
```

目录只包含五种页面类型：

| Page Type | 定义 |
|---|---|
| `home` | 年龄门禁及普通阻断清除后的真实首页 |
| `web_video_list` | 页面主体为重复视频卡片和播放入口组成的列表 |
| `web_video_detail` | 页面主体为单个视频播放器及其元数据 |
| `web_content_list` | 页面主体为非视频内容卡片或条目列表 |
| `web_content_detail` | 页面主体为单个非视频内容详情 |

不得增加 `web_unknown`。证据不足时使用：

```json
{
  "classification_status": "needs_review",
  "page_type": null,
  "coverage_eligible": false
}
```

Catalog 建议顶层合同：

```json
{
  "catalog_id": "web_desktop_v1",
  "platform": "web",
  "version": "<version>",
  "schema_version": "web-page-type-catalog.v1",
  "page_types": []
}
```

每个类型的信号字段包括：

- `url_patterns`
- `negative_url_patterns`
- `menu_keywords`
- `title_keywords`
- `visible_text_keywords`
- `dom_hints`
- `structural_hints`
- `layout_hints`
- `entry_hints`
- `next_page_candidates`
- `coverage_required`
- `rule_matchable`

URL 与菜单文案只能作为辅助信号，目标页面 DOM 和结构证据优先。

## 9. 五类判定规则

### 9.1 home

- 只允许从入口状态机形成；
- 必须 `home_ready=true`；
- 不能由菜单文案或普通 URL 启发式补判。

### 9.2 web_video_list

主要证据：

- 重复视频封面、时长、播放图标和视频详情入口；
- 多个卡片最终指向播放器主导的详情页；
- 页面主体不是单个播放器。

### 9.3 web_video_detail

主要证据：

- 单个主播放器；
- 播放、暂停、进度、音量或全屏控件；
- 页面主体围绕一个视频实体。

### 9.4 web_content_list

主要证据：

- 重复非视频内容卡片或条目；
- 多个普通内容详情入口；
- 页面主体不是播放器。

### 9.5 web_content_detail

主要证据：

- 单个正文、图片集、帖子或专题内容主体；
- 没有主视频播放器；
- 页面围绕一个非视频内容实体。

## 10. 全菜单探索

### 10.1 菜单来源

菜单信息来自两部分，但只有白名单命中的条目可以执行：

1. 操作者提供的六站导航清单；
2. 真实首页和后续菜单页中实际发现的同源导航结构。

操作者提供的清单形成版本化的最低必跑白名单。运行时 DOM 用于证明条目此刻确实
存在、核对层级和发现漂移。DOM 新发现但未在当前版本中的条目只登记为
`runtime_discovered_unreviewed`，不得自动加入可执行 Frontier；审核并发布新版本后
才能执行。

导航发现规则：

- 优先识别语义化 `<nav>`；
- 或元素 `class`、`id` 大小写无关地包含 `nav`；
- 或 `aria-label`、导航相关 `data-*` 与重复链接结构共同证明其为导航；
- `ul/li/a[href]` 是强结构信号，但不是硬性前提；部分站点使用导航容器直属的
  重复 `a[href]`；
- HTML 注释、模板占位和当前不可用节点不进入活动菜单清单；
- 过滤下载、退出、支付、上传等动作；跨域链接保留审计条目但不得执行。

这组规则覆盖附件中的三类实际结构：

- `nav -> ul -> li -> a`；
- `ul[class*="nav"] -> li -> a`，含下拉二级菜单；
- `nav` 语义容器或 `id*="nav"` 容器下的直属重复 `a`。

### 10.2 菜单身份

```text
menu_entry_id =
  site_origin
  + 菜单层级路径
  + 标准化 href
  + 菜单位置
```

禁止使用 `page_type_key` 作为菜单完成键。

URL 规范化必须保留查询参数语义，忽略 fragment，不允许主机通配。HTTP 站点可以
升级到同主机 HTTPS，但 HTTPS 不得降级到 HTTP。菜单文字只用于人工审计和 DOM
一致性核对，不能单独命中白名单。

### 10.3 六站菜单清单

附件原始导航 HTML 的摘要为
`2B9DB035750126C905C1C87829BE47C5C4C34389618FA5091C6C0CF29E82DDF0`。逐条清单保存于
`docs/data/web_v2_six_site_menu_allowlist.json`。

| 站点 | 活动锚点 | 首页基线 | 同源待执行 | 跨域阻断 | 结构说明 |
|---|---:|---:|---:|---:|---|
| `heiliao.com` | 30 | 1 | 29 | 0 | `id` 含 `nav`，直属重复链接 |
| `hl365.com` | 43 | 1 | 42 | 0 | `navbar-nav`，8 个非跳转下拉分组 |
| `www.91jav1.com` | 8 | 1 | 6 | 1 | 语义 `nav` 与 `ul/li/a` |
| `91porna.com` | 65 | 1 | 62 | 2 | 一级链接与下拉子菜单；`/moviesets` 有两个菜单身份 |
| `huangguoai.com` | 9 | 1 | 8 | 0 | 语义 `nav` 下直属重复链接 |
| `avhub360.com` | 11 | 1 | 10 | 0 | `navbar-nav`；另有 4 个注释内链接，不计活动菜单 |
| **合计** | **166** | **6** | **157** | **3** | 162 个唯一同源目标 |

上表是六个独立站点任务的汇总，不代表一次任务同时加载六站菜单。每次 Web 任务只
解析与当前目标 `origin` 精确匹配的一个站点目录：该站首页条目复用已经形成的
`web_home_baseline`，该站同源非首页条目进入菜单 Frontier，该站跨域条目只登记
`cross_origin_blocked`。六次独立任务累计形成 6 个首页、157 个同源执行终态和
3 个跨域阻断终态。若要探索跨域目标，必须把它作为新的站点目标单独建档。

### 10.4 白名单判定

可以把这六站菜单做成白名单，但白名单只授权“受控 GET 导航并观察”，不授权页面
内业务操作。一个条目必须同时满足：

1. 当前任务站点与目录 `origin` 精确匹配；
2. 当前 DOM 中存在相同菜单层级或可解释别名下的真实 `a[href]`；
3. 规范化后的 host、path 和 query 命中当前目录版本；
4. 元素不是 HTML 注释、隐藏模板、下载、退出、支付、上传或表单提交；
5. 导航前页面已经完成阻断清理；
6. 导航及重定向后的最终地址仍为同主机 HTTP(S)，且没有 HTTPS 降级；
7. 导航后重新执行入口 Capture、逐层清障和页面分类。

白名单状态固定为：

- `home_baseline`：复用真实首页，不重复入队；
- `allowlisted_observe_only`：允许受控导航、截图、DOM、分类和可信详情采集；
- `blocked_cross_origin`：不离开当前站点，直接登记阻断终态；
- `allowlist_drifted`：目录条目在当前 DOM 缺失或目标发生变化；
- `runtime_discovered_unreviewed`：DOM 新发现但当前版本未收录，只记录不执行。

白名单命中不得绕过年龄门禁、普通阻断弹窗、`z-index > 500` 候选检查、人机验证
边界或页面就绪门。像投稿、联系、APP 介绍等同源菜单页面可以打开并观察，但不得
提交表单、触发下载或继续点击外部目标。

详情链接不属于菜单白名单。它们只能在某个已放行菜单页完成无遮挡证明后，从重复
卡片 DOM 中动态提取；每次仍需通过同源、可见、Hit Test 和非业务动作校验。

### 10.5 重复 URL

两个菜单即使指向同一个 URL，也必须分别执行以证明菜单路径有效：

- 菜单覆盖记两个完成；
- 页面指纹相同可以共享唯一页面样本；
- Catalog 统计不重复计算同一页面；
- 审计保留两个菜单来源。

### 10.6 菜单页和详情页

每个菜单入口：

```text
执行菜单
  -> Capture 页面入口截图和 DOM
  -> 使用截图、DOM 与 effective z-index>500 候选逐层清障
  -> 确认不存在剩余阻断
  -> Capture page_ready_baseline
  -> 页面类型判定
  -> 如果是列表页，从 DOM 提取至少一个可信详情入口
  -> Capture 详情页
  -> 对详情页重复逐层清障
  -> 判定详情类型
  -> 返回并继续下一菜单
```

菜单直接进入详情页时，采集该详情后即可进入菜单终态。

详情入口必须来自当前无遮挡 DOM，至少满足：

- 位于重复卡片、列表条目或明确详情入口结构中；
- 同源 HTTP(S)；
- 当前可见、矩形有效且 Hit Test 命中自身或可信可点击祖先；
- 不是菜单、分页、面包屑、登录、下载、支付或广告链接；
- 目标 URL 或结构信号与列表类型一致。

`web_video_list` 优先选择通向播放器主导页面的卡片链接；
`web_content_list` 优先选择通向单个非视频内容主体的卡片链接。没有可信 DOM
详情候选时登记 `no_detail_candidate`，禁止由 LLM 补造 URL。

### 10.7 完成条件

```text
menu_frontier 为空
AND
当前站点的首页条目已绑定 home baseline
AND
当前站点的全部同源非首页白名单条目均有执行终态
AND
当前站点的全部跨域条目均有 cross_origin_blocked 终态
AND
所有运行时新发现或漂移条目均有非执行审计终态
AND
每个成功列表菜单完成详情入口检查
```

六站验证活动在六个单站任务都满足上述条件后，再汇总校验 6 个首页、157 个同源
执行终态和 3 个跨域阻断终态。

绝不使用“已出现五种 Page Type”作为完成条件。

## 11. 菜单终态

- `detail_captured`
- `menu_page_captured`
- `no_detail_candidate`
- `popup_blocked`
- `age_gate_needs_review`
- `age_attestation_required`
- `human_verification_blocked`
- `unreachable`
- `timeout`
- `unsafe_navigation`
- `cross_origin_blocked`
- `allowlist_drifted`
- `runtime_discovered_unreviewed`
- `classification_needs_review`

单个菜单失败后继续下一个菜单；只有入口首页无法形成时整站不得进入菜单阶段。

## 12. 限额与防循环

- 同一页面同一门禁指纹最多确认一次；
- 单页面最多执行一次年龄确认；
- 同一站点会话最多执行两次年龄确认；
- 相同门禁指纹再次出现时失败关闭；
- 菜单按 Menu Entry ID 去重；
- 页面证据按规范 URL 与页面指纹去重；
- 同 URL 的不同菜单层级路径分别保留 Menu Entry ID；
- 分页、面包屑和内容卡片不进入菜单账本；
- 内容卡片只作为列表到详情的候选。

## 13. 审计合同

年龄确认动作至少记录：

```json
{
  "action_type": "web_age_gate_confirm",
  "authorization_policy": "confirm_adult_entry",
  "age_attestation_status": "self_declared",
  "operator_age_verified": false,
  "capture_id": "capture-id",
  "gate_signature": "sha256:<digest>",
  "model_name": "<model-id>",
  "decision_confidence": 0.96,
  "candidate_id": "candidate-1",
  "selector_source": "sealed_ui_snapshot",
  "before_url": "https://example.com/",
  "after_url": "https://example.com/",
  "gate_removed": true
}
```

菜单账本必须关联：

- Site / Task / Run / Session；
- Menu Entry ID；
- before/after Capture；
- Action Execution；
- 页面类型判定；
- 阻断状态；
- 详情页结果；
- 终态原因。

## 14. 失败处理

| 情况 | 结果 |
|---|---|
| 无年龄门禁 | 继续普通弹窗清障 |
| LLM 判断不确定 | `age_gate_needs_review` |
| 缺少成年自我声明 | `age_attestation_required` |
| 确认按钮不唯一 | 不点击，进入复核 |
| 点击后门禁仍存在 | `age_gate_confirmation_failed` |
| 点击后跨域 | 阻断并关闭未声明页面 |
| 检测到人机验证 | `human_verification_blocked` |
| 首页仍有未知阻断 | 不建立 `home` |
| 菜单页阻断无法解除 | 当前菜单终态失败，继续下一菜单 |
| 详情页无可信入口 | `no_detail_candidate` |
| 白名单条目在 DOM 中缺失或 href 改变 | `allowlist_drifted` |
| DOM 新发现未收录菜单 | `runtime_discovered_unreviewed`，不执行 |
| 白名单初始目标同源但最终重定向跨域 | `cross_origin_blocked` |

## 15. 预期文件边界

新增：

```text
services/api/app/domain/page/web_page_type_catalog.json
services/api/app/domain/page/web_site_menu_catalog.json
services/api/app/application/web_age_gate.py
services/api/app/application/web_page_obstruction.py
services/api/app/application/web_menu_coverage.py
services/api/tests/test_web_age_gate.py
services/api/tests/test_web_menu_coverage.py
services/api/tests/test_web_page_type_catalog.py
services/api/tests/test_web_site_menu_catalog.py
```

修改：

```text
services/api/app/infrastructure/browser/web_desktop_capture_policy.py
services/api/app/application/web_desktop_exploration.py
Web V2 任务配置与组合根
相关 Web 专属测试
```

保持不变：

```text
services/api/app/domain/page/page_type_catalog.json
services/api/app/application/h5_age_verification.py
H5/App Runtime 默认组合
```

## 16. 实施顺序

1. 固定 Web Catalog Schema 和五个 Page Type。
2. 增加操作者成年自我声明合同与 Web 任务门禁。
3. 增加入口探测 Capture 角色。
4. 增加年龄门禁候选提取与 LLM 结构化决策端口。
5. 增加候选按钮安全校验、点击与门禁消失验证。
6. 增加截图、DOM、effective z-index 与 Hit Test 联合阻断判定。
7. 增加每页最多 12 层、120 秒的有界清障循环。
8. 建立真实首页 Baseline。
9. 增加全菜单覆盖账本，移除 Page Type 覆盖提前终止。
10. 将已审计的六站导航清单发布为站点级菜单目录。
11. 完整采集菜单页和每菜单至少一个 DOM 可信详情页。
12. 基于样本完善 `web_page_type_catalog.json` 信号。
13. 以 Shadow 模式验证页面分类。
14. 验证 H5/App 回归与 Web 独立 Worker 资源。

## 17. 测试策略

### 17.1 年龄门禁

- 文字型、图片型和多语言年龄门禁；
- 唯一确认按钮与多个歧义按钮；
- “已成年进入”和“未满18退出”方向识别；
- LLM 返回不存在、过期或错误 Capture 候选；
- 页面指纹漂移；
- 点击后门禁消失、未消失、跨域和新窗口；
- 缺少操作者声明时拒绝点击；
- 相同门禁重复出现时防循环；
- 人机验证只阻断，不执行处理。

### 17.2 首页

- 有门禁时入口 Capture 不计 `home`；
- 无门禁无遮挡时允许升级为首页 Baseline；
- 执行动作后必须重新 Capture；
- 门禁或未知阻断仍存在时禁止菜单发现。
- 普通弹窗关闭后重新执行年龄门禁检测。
- 高 z-index 非阻断 Header、Toast 和角标不会被误关。
- 年龄门禁位于普通弹窗下层时按层清理后仍能识别。

### 17.3 页面目录

- Catalog 只有五种 Page Type；
- Web 显式加载 Web Catalog；
- Web Catalog 失败时不回退 H5；
- 证据不足返回 `needs_review`，不增加第六种类型；
- URL、菜单文案不能单独签发高置信类型。

### 17.4 全菜单

- 同 Page Type 的多个菜单全部执行；
- 同 URL 的不同菜单路径分别执行；
- 单个菜单失败后继续下一菜单；
- 输入白名单与运行时发现菜单对账，未收录条目不合并进可执行 Frontier；
- 六站目录共 166 个活动锚点，拆分为 6 个首页、157 个同源待执行和 3 个跨域阻断；
- 每次任务只加载当前站点目录，六站汇总数字只在六个单站任务全部结束后计算；
- 运行时未收录菜单只审计不执行；
- 白名单条目缺失或 href 漂移时不使用旧 URL 强行直达；
- 菜单文字相同但 URL 不同、URL 相同但菜单路径不同均保持独立身份；
- HTML 注释内的链接不会进入活动菜单；
- HTTP 只允许同主机升级 HTTPS，不允许 HTTPS 降级或跨主机重定向；
- Page Type 全部出现时仍继续未完成菜单；
- 所有菜单终态且 Frontier 为空时才结束；
- 每个成功列表菜单完成详情入口检查。
- 每个菜单页和详情页都在无遮挡后才分类。
- 阻断链可连续清理多层，并保存逐层 before/after 证据。
- 超过 12 层、120 秒或签名不变时失败关闭当前页面。
- 详情入口来自真实 DOM，不接受 LLM 生成 URL。

### 17.5 隔离

- H5 Catalog、H5 年龄确认和 H5 捕获默认值不变；
- App Runtime 不注入 Web 年龄门禁；
- Web 门禁功能关闭时旧 H5/App 结果不变；
- Web LLM 识别失败不占用 H5/App Worker。

## 18. 验收标准

1. 首次入口探测证据与真实首页证据角色完全分离。
2. 年龄门禁未解除时不会登记 `home` 或开始菜单探索。
3. 缺少操作者主动成年声明时不会点击确认按钮。
4. LLM 只返回封存候选 ID，不返回选择器或坐标。
5. 不实现任何人机验证、验证码或 Cloudflare 绕过。
6. 真实首页形成后才提取菜单。
7. Web Catalog 只包含确认的五种 Page Type。
8. 所有同源白名单菜单均执行，跨域与漂移菜单均有明确终态，不因 Page Type 已覆盖提前结束。
9. 每个成功列表菜单至少采集一个可信详情页，或登记无详情原因。
10. 所有年龄确认和菜单动作具备可追溯审计链。
11. Web Catalog 与 H5 Catalog 物理、加载和语义隔离。
12. H5/App 相关自动化回归全部通过。
13. 首页、菜单页和详情页都在截图、DOM 与 effective z-index 联合证明无遮挡后才开始探索。
14. `z-index > 500` 只生成阻断候选，不会单独触发盲点关闭。
15. 清障后必须重新 Capture，且剩余阻断时不得计入页面覆盖。
16. 详情页只能通过当前无遮挡 DOM 中的可信同源入口进入。
17. 页面类型 Catalog 与站点菜单 Catalog 独立加载、独立版本化且都不回退 H5。
18. 运行时新发现菜单未审核前不会自动执行，白名单也不会放行业务提交或下载动作。
