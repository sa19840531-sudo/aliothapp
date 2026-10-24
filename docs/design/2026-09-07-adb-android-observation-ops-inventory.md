# ADB Android 页面观察与操作能力清单（现况）

> 日期：2026-09-07（观察／操作能力定稿补充：2026-09-08；页面稳定 quorum #5：2026-09-09）  
> 状态：现况整理已定稿（对齐生产代码；§2＝观察卡片；§3＝操作卡片；§2.7＝页面稳定判断）  
> 范围：Android App / 云手机 ADB；不含 H5 Playwright  
> 需求表锚点：§2＝整理 ADB 页面观察能力；§3＝整理 ADB 页面操作能力（含坐标换算）；§4＝App 探索 Skill；§2.7＝完善 App 页面稳定判断  
> 关联：[`06-interaction-precision.md`](./2026-09-03-exploration-optimization/06-interaction-precision.md)、[`05-capture-readiness.md`](./2026-09-03-exploration-optimization/05-capture-readiness.md)、[`app-workflow/SKILL.md`](../../services/api/app/agents/skills/app-workflow/SKILL.md)、[`专项能力与高频回归规则.md`](../专项能力与高频回归规则.md)

## 1. 结论（先看这里）

1. **观察不是单一 API**：截图＋前台身份、UIA 树、dumpsys View Hierarchy、视觉理解分属不同入口；探索主事实是**截图**，UIA 是门禁／点击解析辅助。
2. **「统一获取」指契约统一，不是同帧原子包**：前台 App、截图、屏幕尺寸、方向、可用结构分别有确定入口与返回形（§2.2）；本轮**不做** `observe_all`。
3. **操作原语已统一**：`tap`／`swipe`／`back`／`type_text` 只允许经 `AdbDeviceOps`；Explorer 只能选 `candidate_id`，坐标由 Runtime 解析。
4. **租约与目标包已强制**：无 Session Command Permit、未绑定 `allowed_packages`、动作前前台身份漂移 → 失败；不得在外部包继续业务点击。
5. **坐标换算（已实施＋旋转加深）**：`map_point`／`map_point_to_device` 在宽高比显著不一致时按居中 letterbox 映射；`safe_area` insets 接入落点校验（系统栏误点 **拒绝**）；`query_display_rotation` 可证明时写入审计并在「自然方向截图 vs 当前方向设备」错位时做 Surface 仿射；未证明仍标 `rotation_unproven`。
6. **同帧观察捆（已实施）**：`capture_same_frame_observation`／Driver `capture_same_frame` 紧邻截图＋UIA，共享 `observation_epoch`；**不**声称内核级原子，但禁止把跨会话旧树当该截图证明。
7. **surface_tech（有界推断）**：凭 UIA 标记推断 `native`／`webview_shell`／`flutter`／`unknown`；`authority=false`，无树不瞎猜。

---

## 2. 页面观察能力

> **需求表项**：整理 ADB 页面观察能力（Native／WebView／Flutter 结构矩阵）。

### 2.1 通道矩阵（Native / WebView 壳 / Flutter App）

| 通道 | 产出 | Native | WebView 壳 | Flutter App |
|------|------|--------|------------|-------------|
| 前台身份 | `package`／`activity`（`dumpsys activity activities` + `dumpsys window`） | 可用 | 可用 | 可用；若 MoreLogin Agent 对 dumpsys 回「Command not found」须回退真实 ADB |
| 截图 | PNG 字节 + 像素宽高；orientation 由高宽推导 | **探索主事实** | **探索主事实** | **探索主事实** |
| Accessibility UIA | `uiautomator dump` → 节点树（bounds／text／content-desc／clickable） | 常可用，供门禁与精确语义点击 | 全屏空 `android.webkit.WebView` 壳不算 meaningful content | 常贫瘠或整屏可点；不可假设结构化可用 |
| View Hierarchy | `dumpsys activity top` 等；契约词 `view_hierarchy` | 辅助 | 辅助 | 辅助；**不得**用 ui_tree 冒充 |
| 视觉理解 | `AppVisualAnalysis`（页型／遮挡／候选框） | 探索主语义 | 探索主语义 | 结构化不可用时几乎唯一替代 |

**没有**把 `surface_tech` 写成 Gate／Coverage 权威字段。有界推断见 `android_surface_tech.infer_android_surface_tech`；无 UIA 时恒 `unknown`。

### 2.2 入口与返回形（现码）

| 入口 | 文件 | 返回要点 |
|------|------|----------|
| Official `POST /apps/capture` | `xingyu_runtime/device_bridge.py` → `CaptureShot` | PNG；headers：`width`／`height`／`orientation`／`package`／`activity`；截图前后读身份，不一致则重截 |
| Official `POST /apps/capture-ui-tree` | 同上 `dump_ui_tree` | raw Accessibility XML；与 capture **非原子同帧** |
| `AndroidAdbSurfaceDriver.capture` | `android_surface_driver.py` | 仅 screenshot artifact + display／identity metadata |
| `AndroidAdbSurfaceDriver.observe` | 同上 | `RawSurfaceObservation`：artifact 计数／display／identity；**不含 PNG、不含 UI tree** |
| `observe_runtime_state` | 同上 + `android_runtime_observation.py` | 内存 UIA 节点；失败 → `available=False`，不伪造 |
| 视觉管线 | `app_screenshot_exploration.py` | 从已提交截图产出候选；探索主路径 |

底层原语集中在 `infrastructure/device/adb_ops.py`：`screencap`、`current_runtime_identity`、`query_wm_size`／`wm_size`、`dump_ui_hierarchy`、`dump_view_hierarchy`、`dump_display_safe_area`。

### 2.3 必须写清的边界

1. **探索主事实是截图**；UIA 是门禁／解析辅助，**不等于**同帧已持久化证据。
2. **`capture` 与 `capture-ui-tree` 非同帧**：分开调用时树与图可能不一致；不得把跨调用树当作该截图的结构化证明。
3. **Orientation 弱**：由 PNG 像素 `portrait if h>=w else landscape` 推导；**不是** sensor／`dumpsys` rotation。
4. **Capability 双套口径（须分开读）**：
   - `AndroidAdbSurfaceDriver.describe_capabilities()`：observations 仅 `screenshot`＋`ui_tree`（注明 UIA 仅运行时、不保证落库）。
   - `build_android_screenshot_only_manifest()`：另声明 `identity.*`／`orientation`／`ui_tree`／`view_hierarchy`／`safe_area` 等为 ready——表示**原语／契约可调用**，不是「本帧 Capture 一定已持久化该制品」。
   - 尤其 **`view_hierarchy`：原语 `AdbDeviceOps.dump_view_hierarchy` 存在，Official／Xingyu 主路径未默认挂载持久化**；消费方不得假设每帧有落库 View Hierarchy。
5. **`ui_tree` ready ≠ 每帧落库**：Manifest 可声明 ready，但 Capture Writer 主路径仍以 screenshot 持久化为主。
6. **身份质量两套**：Xingyu Official capture 有 before／after 身份稳定重截；SurfaceDriver `capture` 不做同等重试。
7. **无 `surface_tech` 探测器**：不写 `native|webview|flutter` 字段；差异靠「树是否 meaningful + 视觉降级」（§2.1／§2.4）。

### 2.4 有结构 vs 无结构时「能拿到什么」

| 场景 | 能拿到 | 不能假设 |
|------|--------|----------|
| Native 有结构 | 截图 + 常可用 UIA；可用 text／content-desc 精确点 | 树覆盖全部可点业务入口；树与截图同帧（除非同次原子采集） |
| Native 无结构／树失败 | 截图 + 前台身份；视觉候选 | 门禁「有内容」一定成立 |
| WebView 壳 | 截图 + 包名；可能只有空 WebView 节点 | 空壳算业务页完成；有 DOM／调试桥 |
| Flutter App | 截图 + 身份；UIA 可能只有整屏可点或稀疏节点 | 可按原生控件树做详情／底栏；H5 Flutter `flt-semantics` 不在本栈 |

### 2.5 「统一获取」对照表（按事实找入口）

| 要获取的事实 | 推荐入口 | 尺寸／方向从哪来 | 结构从哪来 |
|--------------|----------|------------------|------------|
| 前台 App | Official capture headers／`current_runtime_identity`；SurfaceDriver metadata | — | — |
| 页面截图 | Official `POST /apps/capture`；SurfaceDriver `capture` | PNG IHDR 宽高；orientation＝高≥宽→portrait | 无（像素） |
| 屏幕尺寸 | `query_wm_size`／`wm_size`；capture metadata `display` | 设备逻辑像素 | — |
| 方向 | 截图高宽推导（弱） | 非 sensor | — |
| 可用界面结构 | `capture-ui-tree`／`observe_runtime_state`（UIA）；`dump_view_hierarchy`（辅助，主路径常不落库） | UIA 节点 bounds 用设备坐标 | Native 常有；WebView 壳／Flutter 常无 meaningful 结构 |

验收：能按上表回答「怎么拿」与「拿不到什么」即完成本观察卡片，无需新 Device API。

### 2.6 导航入口发现（现况）

> **需求表项**：完善 App 导航入口发现（结构／文字／图标／视觉区域；来源截图；禁止固定坐标长期入口）。

| 发现通道 | 现况 | `discovery_source` |
|----------|------|-------------------|
| 视觉 LLM（文字／图标／区域框） | 主发现；截图主事实 | `vision` |
| UIA 对齐 Vision 候选 | 同帧树精确命中后改写 bounds | `uia_aligned` |
| UIA 补发受信控件 | skip／dismiss／底栏等 | `uia_issued` |
| 覆盖／底栏／分栏提升 | **只提升当帧真实候选**，不新建几何 | `promoted_*` |
| 开屏默认右上角 | 仅清障兜底；执行后须新 Capture | `clearance_synthetic` |

合同：`app_navigation_entry_provenance.py`；候选 metadata 必含 `source_capture_id`＋`discovery_source`。  
**Android 不做 Flutter Semantics API**（H5 `flt-semantics` 不在本栈）；Flutter App 贫瘠 UIA → 视觉降级。  
等分底栏／包表合成寻页已永久拒绝。

### 2.7 页面稳定判断（需求表 #5）

> **需求表项**：完善 App 页面稳定判断（前台／窗口／结构／连续帧；缺网记缺口，禁止伪造网络稳定）。

| 信号 | 现况入口 | 缺口时行为 |
|------|----------|------------|
| 前台 App | Capture／分析 `package_name` + 包边界 | 越界 → `blocked`，不计业务稳定 |
| 当前窗口 | `activity` + 身份三明治 before／after | 漂移 → 不可业务 ready |
| 界面结构 | `trusted_accessibility_controls`／`ui_hierarchy_available` | unavailable／unknown 写入 `capability_gaps`；Flutter 贫瘠不伪造空树成功 |
| 连续帧 | `image_signature` 相对 `app_page_stability_frame_digests` | 变化 → `transitional`；样本不足单独标明 |
| 网络 | 默认不可得（无 HTTPS 生命周期） | `network_stable` **恒 false**；缺口码 `network_lifecycle_unavailable`；**禁止**把无请求当空闲 |

实现：`build_app_page_stability_assessment`／诊断 v4；coverage 写 `app_page_stability_assessment`。  
**正式** `formal_settle_oracle` 仅在显式 `ALIOTH_APP_MILESTONE_AUTHORITY_M1=true` 时打开，并写 `settle_enforcement_preview`；**`gate_consumes_settle`／Gate SQL 仍关闭**。开闸评审见 [`2026-09-09-app-m1-formal-settle-oracle-review.md`](./2026-09-09-app-m1-formal-settle-oracle-review.md)。

---

## 3. 页面操作能力

> **需求表项**：整理 ADB 页面操作能力 + 补坐标换算（旋转／系统栏／云手机黑边／解析度）。  
> **定稿**：2026-09-08（代码与单测已对齐；权威 display rotation 仿射校正仍为缺口）。

### 3.1 四动作契约

| 动作 | ADB 原语 | 入口 | 参数要点 |
|------|----------|------|----------|
| `tap`／`click` | `input tap x y` | `AdbDeviceOps.tap` ← Driver `execute` | 设备像素点；`click` 由 `action_normalize` 归一为 `tap` |
| `swipe`／`scroll` | `input swipe …` | `swipe`；方向式 scroll 由 Resolver 算中线轨道 | 起止点均为设备像素 |
| `back` | `keycode 4` | `back()` | 无坐标；外跳／不稳定页恢复主手段 |
| `type_text` | ASCII：`input text`；CJK：剪贴板 + `KEYCODE_PASTE(279)` | `type_text` | 可先 tap 聚焦；正式 Explorer 默认不选 input |

调用链：`action_normalize` → `CanonicalPointTargetResolver` → `AndroidAdbSurfaceDriver.execute` → `AdbDeviceOps`。  
Explorer **不得**自造坐标、CSS、XPath、Appium locator。

### 3.2 坐标空间（`map_point`／`map_point_to_device`）

定义于 `adb_ops.py`；canonical 默认 `1080×1920`。Resolver 经 `CanonicalPointTargetResolver.from_device_ops` 读取 `wm size` 与 `dump_display_safe_area`。

| space | 行为 |
|-------|------|
| `device_px` | 原样取整；仍过设备边界与 safe_area 校验 |
| `normalized_01` | × `device_w`／`device_h` |
| `capture_px` | 相对截图 IHDR `base_w×base_h`；宽高比与设备差 ≤2% 时纯等比，否则居中 letterbox 内容区→设备 |
| `canonical_px` | 相对 canonical 基尺寸；同上 letterbox 规则 |

换算契约：

- 点落在 letterbox **黑边内** → fail-closed（`ANDROID_POINT_IN_LETTERBOX`），不猜测。
- 落点在系统栏 inset 外 → **拒绝**（`ANDROID_POINT_OUTSIDE_SAFE_AREA`），本轮不 silently 夹到 safe 区。
- 无 dumpsys／sensor 权威 rotation 时：`mapping_audit.rotation_unproven=true`；不得假装已校正横竖。
- 视觉候选应标 `capture_px` + 真实截图宽高；禁止把截图缩图当 `device_px`（见专项规则 2026-09-01／letterbox）。
- UIA 命中时用 **device 边界中心**，不再走视觉点；a11y bounds 校验用 hierarchy／设备尺寸，不用缩图宽高。
- 整屏 Dismiss／同名蒙层须过滤，禁止取屏幕正中当关闭点（见专项规则 2026-09-02）。
- 动作审计可写 `coordinate_mapping`（`letterboxed`／`rotation_unproven`／内容矩形）。

### 3.3 租约与目标 App 绑定（平台已强制）

| 层 | 机制 | 失败表现（示例） |
|----|------|------------------|
| 设备租约 | `device_pool` claim／renew；lease_owner + token | 租约不匹配拒绝执行 |
| 会话串行 | `SessionCommandGate` → Permit | 无 Permit 不得裸 `execute` |
| 包绑定 | APP_READY `bind_installed_package` → `allowed_packages` | 实际安装包不在集合 |
| 动作前身份 | `_require_runtime_identity`：before Capture 包 ∈ 允许集；dumpsys 前台与 Capture 一致 | `APP_PACKAGE_BOUNDARY_VIOLATION`／`APP_RUNTIME_IDENTITY_UNAVAILABLE`／`STALE_APP_RUNTIME_IDENTITY` |
| 有限例外 | 受控 `system_dialog`；包外允许的 `back` 等恢复 | 非例外路径不得在外部包业务点击 |

### 3.4 目标解析优先级

1. **UIA 精确语义**（可见、可用、可点击、非整屏蒙层）命中 → 用节点中心（device_px）。
2. 否则使用视觉合同约束的 **`capture_px` point**（由 Runtime 从 `candidate_id`／`resolved_target` 落下）。
3. 无唯一精确命中且无合法视觉点 → 不得猜测。

清障控件允许平台容差重绑（`can_rebind_visual_obstruction_control`）；**业务目标**在 scroll／swipe 后必须等新 Capture，不得沿用旧框。

### 3.5 「统一操作」对照表（按意图找入口）

| 意图 | 唯一执行入口 | 租约／目标 App | 坐标从哪来 |
|------|--------------|----------------|------------|
| 点击 | `AdbDeviceOps.tap`（经 Driver `execute`） | 须有 Permit + `allowed_packages`；动作前身份校验 | Resolver：UIA 中心或 `capture_px`→`map_point_to_device` |
| 滑动／滚动 | `AdbDeviceOps.swipe` | 同上 | 起止点均为设备像素（方向式 scroll 由 Resolver 算中线） |
| 返回 | `AdbDeviceOps.back` | 包外仅恢复路径放行 | 无坐标 |
| 输入 | `AdbDeviceOps.type_text` | 同上；正式 Explorer 默认不选 | 可先 tap 聚焦 |

禁止：绕过 Driver 直连 ADB、Explorer 自造坐标／locator、无租约裸执行、在外部包继续业务点击。

### 3.6 实施状态（已实施／仍缺口）

对照 `06-interaction-precision.md` §3.3；本卡片要求的四动作统一、租约绑定、黑边／系统栏／解析度换算已落地。

| 项 | 状态 | 说明 |
|----|------|------|
| 四动作统一入口 | **已实施** | 仅经 `AdbDeviceOps`；见 §3.1／§3.5 |
| 租约＋目标 App 绑定 | **已实施** | 见 §3.3 |
| 云手机黑边／letterbox | **已实施** | 宽高比差＞2% → 居中内容矩形；黑边内点拒绝；审计 `letterboxed` |
| 系统栏 insets | **已实施** | `dump_display_safe_area` 接入 Resolver；inset 外拒绝误点 |
| 解析度／capture 基准 | **已实施**（既有） | `capture_px` + `base_width`／`base_height`；与 hierarchy 设备尺寸一致 |
| 旋转校正 | **已实施（可证明时）** | `query_display_rotation`＋仿射；未证明仍 `rotation_unproven` |
| 同帧结构＋截图 | **已实施（同帧绑定）** | `app-same-frame-observation.v1`；非内核原子 |
| 显式 `surface_tech` | **已实施（有界推断）** | UIA 标记推断；权威仍 false |

回归锚点：`test_android_surface_driver.py` 中 `map_point_*`／`resolver_*letterbox*`／`resolver_rejects_safe_area*`。

---

## 4. 与 App 探索 Skill 的分工

> **需求表项**：App 探索 Skill（结构优先／视觉降级／滑后重定位／系统弹窗／外跳／back）。由 `app-workflow` **1.5.0** 承担，不另建 `app-navigation` Skill；1.5.0 另含长图／广告外跳三联／收银台长链／播放器连续拖动证据角色合同（`app-evidence-role.v1`）。

| 职责 | 谁负责 |
|------|--------|
| 选哪个 `candidate_id`、何时 back／observe | Agent + `app-workflow` Skill 纪律 |
| 解析坐标、租约、包边界、执行 ADB、采集前后 Capture | Runtime／Driver（平台强制） |
| H5 URL／DOM／goto | **不是**本清单；见 `page-exploration` |

操作规程正文写在 `services/api/app/agents/skills/app-workflow/SKILL.md`「探索操作规程」；本文件供研发／复审对齐现码，**不随 Agent 镜像依赖阅读**。

---

## 5. 关键文件（1 行）

| 路径 | 角色 |
|------|------|
| `services/api/app/infrastructure/device/adb_ops.py` | ADB 原语与 `map_point` |
| `services/api/app/infrastructure/device/android_surface_driver.py` | Surface Driver capture／observe／execute |
| `services/api/app/infrastructure/device/android_target_resolver.py` | 语义／视觉目标解析 |
| `services/api/app/infrastructure/device/android_runtime_observation.py` | UIA → 内存观察；WebView 壳排除 |
| `services/api/app/application/xingyu_runtime/device_bridge.py` | Official CaptureShot／dump UI／身份稳定重截 |
| `services/api/app/application/app_interaction_runtime.py` | 租约上下文、包边界、清障重绑 |
| `services/api/app/application/app_screenshot_exploration.py` | 截图探索主路径 |
| `services/api/app/agents/skills/app-workflow/SKILL.md` | App Explorer 正式 Skill |

---

## 6. 验收（整理交付）

- 新人能在 10 分钟内回答：Native／WebView／Flutter 各靠什么、什么不能当证据（§2.1／§2.4）；五类观察事实分别从哪拿（§2.5）。
- 新人能回答：点击／滑动／返回／输入唯一入口、租约／目标 App 如何绑定、letterbox／safe_area／rotation_unproven 契约（§3.1–§3.6）。
- 四动作、坐标空间、letterbox／safe_area 契约、租约／包门禁与现码一致（§3）；同帧 ui-tree 与权威旋转校正标为仍缺口（§3.6）。
- Skill 规程与需求表对应，且标清 Skill 指导 vs 平台强制（§4／`app-workflow`）；旁注 `adb-operations.md` 与清单一致（无过期「letterbox 未接入」表述）。
- Manifest 双套口径与 `view_hierarchy` 非默认可信落库已写清（§2.3）；不引入第二套 Device API；不改探索主路径结构。
