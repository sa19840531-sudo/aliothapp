# H5 逻辑页面状态与 SPA / Flutter Web 探索设计

日期：2026-08-28

状态：设计与受限视觉坐标导航口径已确认；待按实施计划分批实现

## 1. 背景

Alioth 当前 H5 探索链已经具备不可变 Capture、DOM、可见文本、截图、`ui_snapshot`、页面识别、Frontier、受控动作以及 before→action→after 证据链。普通多页 H5 可以较稳定地通过 URL、document 导航和 DOM 结构识别页面。

DOM 型 SPA 与 Flutter Web 改变了页面身份的表现形式：

- Vue、React、Angular 等 SPA 可以在同一 document 和同一 URL 下切换完整业务页面；
- Flutter Web 的主界面可能由 Canvas 渲染，普通 DOM 仅保留壳层，可信交互信息主要来自 Flutter Semantics；
- Flutter 页面也可能嵌入真实 HTML 输入框、iframe、广告或 platform view，不能完全抛弃 DOM；
- 弹窗、加载、分页、筛选、轮播和滚动会改变可见状态，但不应膨胀为新的业务页面覆盖。

因此，本设计不建立独立 SPA Explorer 或 Flutter Explorer，而是在现有 H5 主链内增加“统一逻辑页面状态 + 渲染证据适配器”。

## 2. 目标

1. 普通多页 H5、DOM 型 SPA、Flutter Web 和混合页面共用一套 Capture、Frontier、Runtime、审计和报告协议。
2. URL 不变时，可以通过可信结构变化识别新的逻辑业务页面。
3. 页面身份与页面表面状态分离，避免 SPA 状态爆炸。
4. Flutter 优先使用官方 Semantics；语义贫瘠的 Flutter / Hybrid 页面允许截图和 OCR 作为主识别证据，并在受限白名单内签发密封视觉坐标导航能力。
5. 普通多页 H5 继续使用现有状态版本、候选顺序和导航判定，不因新能力发生行为漂移。
6. 所有动作仍经过来源 Capture 绑定、目标唯一性、实时复验、风险策略和操作后证据验收。

## 3. 非目标

- 本期不建立独立 SPA 或 Flutter Worker。
- 本期不开放通用截图坐标点击；唯一例外是满足渲染门禁、白名单、密封能力和动作前实时复验的 `visual_navigation_coordinate`。
- 本期不通过域名、框架品牌或单个脚本文件硬编码页面能力。
- 本期不把筛选、分页、轮播、滚动和弹窗计为新的页面覆盖。
- 本期不改变公开任务 API、现有页型目录或规则匹配契约。
- 本期不要求数据库迁移；新增结构优先写入现有 Capture 和动作 metadata。

## 4. 已确认的核心口径

只有以下变化可以产生新的逻辑页面：

- 主内容骨架变化；
- 激活的主导航变化；
- 页面根、标题或返回结构变化；
- 实际目录 `page_type` 变化并得到独立结构证据支持。

以下变化只属于页面状态：

- 弹窗、遮罩、toast、验证码；
- loading、刷新、倒计时；
- 分页、筛选、排序；
- 轮播、广告资源和列表数据更新；
- 滚动位置；
- Flutter 动态语义节点 ID 重建；
- URL query/hash 改变但主内容身份未变。

覆盖统计只按逻辑页面身份计算；Capture、动作验效和阻断分析仍保存每次表面状态变化。

## 5. 方案选择

采用“统一逻辑状态模型 + 渲染证据适配器”。

未采用的方案：

1. 在现有检测器内持续增加 SPA / Flutter 条件分支：初期改动小，但会让页面识别、弹窗、导航和运行时复验继续耦合。
2. 建立独立 SPA / Flutter Explorer：隔离明显，但会复制 Capture、Frontier、预算、审计和报告协议，破坏当前稳定边界。

## 6. 总体架构

```text
现有 H5 Capture
  │
  ├─ screenshot / DOM / visible_text / ui_snapshot
  │
  ▼
RenderProfileDetector
  ├─ dom_unclassified
  ├─ multipage_dom
  ├─ dom_spa
  ├─ flutter_web
  ├─ hybrid
  └─ unknown
  │
  ▼
Evidence Adapters
  ├─ LegacyDomEvidenceAdapter
  ├─ DomSpaEvidenceAdapter
  ├─ FlutterSemanticsEvidenceAdapter
  └─ VisualSupplementAdapter
  │
  ▼
LogicalSurfaceNormalizer
  ├─ identity_signals
  ├─ state_signals
  ├─ action_targets
  └─ evidence_quality
  │
  ▼
现有 Page Detection → Frontier → Runtime → before/action/after
                                 ▲
                                 │ 仅 flutter_web / hybrid + semantics_sparse
VisualNavigationDetector → VisualTargetPolicy → VisualTargetCapabilitySigner
                                 │
                                 ▼
                    VisualCoordinateResolver → 浏览器 XY 点击
                                 │
                                 ▼
                    VisualTransitionVerifier
```

适配器只负责从已采集证据中提取事实，不负责执行动作，不直接决定业务页型，也不持有浏览器对象。视觉目标签发、坐标解析和点击后验效分别由独立组件承担，不把模型坐标直接交给浏览器。

## 7. 模块边界

### 7.1 RenderProfileDetector

职责：根据当前 Capture 和同一 Surface Session 的前后事实判断渲染类型。

输入：

- DOM 与 `ui_snapshot`；
- Flutter 可访问性激活结果；
- document 身份；
- before / after 主结构差异；
- 当前 Surface Session 内已确认的渲染事实。

输出：

```json
{
  "kind": "dom_spa",
  "confidence": 0.96,
  "signals": [
    "same_document_after_action",
    "primary_navigation_changed",
    "main_content_skeleton_changed"
  ],
  "stable": true
}
```

约束：

- `flt-semantics-host`、唯一官方可访问性占位节点或 Flutter 渲染根属于强 Flutter 信号；
- `main.dart.js`、`flutter_bootstrap.js`、Canvas 和截图外观只能作为辅助信号；
- 不依赖 React / Vue 开发属性，因为生产构建可能移除；
- 普通 DOM 首帧标记为 `dom_unclassified`，不提前猜测是否 SPA；
- 同一 document 内发生可信逻辑页面变化后，才确认 `dom_spa`；
- document 身份变化且满足现有导航事实时，确认 `multipage_dom`；
- Flutter 与有效非 Flutter 交互 DOM 同时存在时，标记为 `hybrid`。

document 身份使用不透明摘要表示，摘要材料固定为当前 `surface_session_id`、浏览上下文页实例和 `performance.timeOrigin`。同一 SPA document 内保持稳定；完整 document 导航后必须变化。URL、DOM 内容和任何用户数据不进入 document 身份。

### 7.2 Evidence Adapters

#### LegacyDomEvidenceAdapter

复用现有 DOM、URL、标题、可见文本和结构化导航事实。普通多页 H5 必须保持当前行为。

#### DomSpaEvidenceAdapter

提取：

- 主导航组及激活项；
- 页面根 landmark；
- 标题、返回结构和稳定头部；
- 列表、详情、表单、视频、个人中心等主内容骨架；
- 稳定业务控件组；
- 同一 document 内的结构变化。

#### FlutterSemanticsEvidenceAdapter

提取：

- `flt-semantics` 的角色、标签、可点击性、禁用状态；
- 父子层级、同级顺序、可见矩形、层级和中心命中；
- Flutter 主导航组、页面根、标题、返回结构和主要语义组；
- Flutter Semantics 激活状态和证据质量。

动态 `flt-semantic-node-*` ID 不进入稳定身份，也不能跨 Capture 作为业务目标。

Semantics 激活结果必须进一步输出覆盖档位，不能只记录 `already_enabled`：

```json
{
  "semantics_coverage_level": "semantics_sparse",
  "usable_node_count": 9,
  "interactive_node_count": 3,
  "semantic_text_char_count": 6,
  "ocr_text_char_count": 84,
  "semantic_ocr_text_coverage": 0.07
}
```

`usable_node_count` 只统计可见、非空且具有稳定角色、标签或可点击性的节点；动态节点 ID、正文重复节点和空角色不计。`semantic_ocr_text_coverage` 使用规范化后能在 OCR 可见文字中对齐的语义字符数除以 OCR 可见字符数。

v1 分档固定为：只有 `usable_node_count >= 12`、`semantic_text_char_count >= 20`，并且在 `ocr_text_char_count >= 40` 时 `semantic_ocr_text_coverage >= 0.35`，才记为 `semantics_sufficient`；激活失败、零可用节点或任一条件不满足均记为 `semantics_sparse`，同时保存具体降级原因。OCR 不可用时，只使用前两个条件，不能用缺失 OCR 反向证明语义充足。

#### VisualSupplementAdapter

从当前 Capture 已有截图和可选 OCR 结果中提取：

- 页面稳定区域布局；
- 主导航视觉选中态；
- 全屏广告、弹窗、空白加载等表面状态；
- DOM / Semantics 未暴露的可见文字。

视觉证据可以补充页面识别。该适配器自身不能签发动作；只有后续 `VisualNavigationDetector` 和 `VisualTargetPolicy` 同时通过时，才能生成受限视觉坐标候选。

### 7.3 视觉坐标导航组件

#### VisualNavigationDetector

仅在 `render_profile in {flutter_web, hybrid}` 且 `semantics_coverage_level=semantics_sparse` 时运行。它消费同一 Capture 的视口截图和 OCR，识别：

- 底部主导航；
- 低风险内容卡片和视频卡片；
- 已确认 `profile` 页内的设置、签到和支付入口。

输出只包含候选矩形、标签、目标类别、来源页型、预期页型和证据摘要，不直接生成浏览器动作。

#### VisualTargetPolicy

只允许以下业务导航：

- 主导航：`home`、`content_list`、`video_list`、`ranking`、`post_list`、`profile`；
- 内容入口：`content_detail`、`video_detail`；
- Profile 专属入口：`settings`、`payment`、`activity`。

登录提交、表单确认、下载、外链广告、删除、授权和交易提交必须拒绝。`payment` 只允许进入页面，不允许确认、充值或提交交易。弹窗和开屏关闭继续使用现有受信关闭能力，不能因 OCR 命中“关闭”或“跳过”而获得通用视觉点击。

#### VisualTargetCapabilitySigner

把当前 Capture、截图 Artifact、视口参数、目标矩形、局部图像特征、OCR 标签、来源状态和预期页型封装为不可变 `visual_navigation_coordinate` 能力。能力完整通过后才能进入现有 Frontier。

#### VisualCoordinateResolver

执行前读取最新 Capture 和视口截图，复验来源逻辑页面、遮挡、视口、标签、局部特征、周边锚点与唯一性。验证通过后把图像坐标转换为视口 CSS 像素，并通过浏览器 XY 点击。若只有目标位置发生可恢复漂移，允许重新识别并重新签发一次；不能复验时失败关闭。

#### VisualTransitionVerifier

点击后获取稳定的 after Capture，以视觉选中态、主体区域、OCR / 布局页型证据为主，DOM、Semantics、URL 和 history 为辅助，判断逻辑页面变化、表面状态变化、无效果或证据不足。

### 7.4 LogicalSurfaceNormalizer

职责：把不同适配器事实转换为渲染器无关的逻辑页面证据。

输出放入现有 Capture metadata：

```json
{
  "logical_surface": {
    "schema_version": "logical-surface.v1",
    "render_profile": "dom_spa",
    "document_identity": "opaque-document-id",
    "identity_fingerprint": "sha256:...",
    "state_fingerprint": "sha256:...",
    "identity_signals": {
      "primary_navigation": {},
      "page_root": {},
      "header": {},
      "content_skeleton": {},
      "stable_control_groups": []
    },
    "state_signals": {
      "overlay_stack": [],
      "loading": false,
      "filter_state": {},
      "pagination_state": {},
      "scroll_bucket": 0
    },
    "evidence_sources": ["dom", "ui_snapshot"],
    "evidence_quality": "trusted",
    "degraded_reasons": []
  }
}
```

## 8. 两层指纹

### 8.1 逻辑页面身份指纹

```text
identity_fingerprint
  = 主导航结构与激活项
  + 页面根结构
  + 标题或返回结构
  + 主内容骨架
  + 稳定业务控件组
```

身份指纹必须排除：

- 动态节点 ID、随机 class、Vue / React 运行时属性；
- 时间、金额、计数、账号 ID、UUID；
- 用户生成标题与列表正文；
- 轮播图片、广告 URL 和动态资源地址；
- 精确滚动位置和一两像素布局变化。

列表数量归一为 `0 / 1 / 2-5 / 6-20 / 20+`，几何位置按固定小范围量化。

`identity_fingerprint` 是持久化字段名；“逻辑页面指纹”只作为其中文业务名称，不再定义第二个同义字段。

### 8.2 页面表面状态指纹

```text
state_fingerprint
  = identity_fingerprint
  + 弹窗栈
  + loading
  + 筛选 / 分页
  + 轮播状态
  + 滚动区间
```

`state_fingerprint` 是持久化字段名。它用于动作验效、遮挡恢复、重复状态收口和报告，不增加业务页面覆盖。

### 8.3 普通多页 H5 兼容

`multipage_dom` 继续使用现有 `state_version` 和导航事实。新逻辑页面指纹只作为可选审计 metadata，不改变其候选 ID、候选顺序和访问去重结果。

只有确认的 `dom_spa`、`flutter_web` 或 `hybrid` 才让逻辑页面指纹参与状态身份。疑似类型只记录，不改变动作和覆盖。

对已确认的 `dom_spa`、`flutter_web` 和 `hybrid`：

- Frontier 的状态版本材料增加 `identity_fingerprint`；
- 表面状态和可信遮挡继续作为状态版本的状态部分；
- visited 页面身份按 `identity_fingerprint` 去重；
- 页面类型覆盖只统计 `detection_status=recognized` 且绑定唯一 `identity_fingerprint` 的实际 `page_type`；
- 同一身份的页型识别冲突不能重复增加覆盖，必须保留冲突并转入重观察或人工复核。

### 8.4 置信度分域与联合门禁

逻辑页面能力不能把“识别出 Flutter / SPA”误当成“识别出具体业务页型”。实现必须保留四个互不替代的信任域：

| 信任域 | 数据形态 | 负责回答 | 不得用于 |
| --- | --- | --- | --- |
| `page_type_confidence` | `0..1` 数值 | 当前业务页型是否可靠 | 证明发生了逻辑页面切换 |
| `render_profile_confidence` | `0..1` 数值 | 当前是否为 DOM SPA、Flutter Web 或 Hybrid | 抬高具体业务页型置信度 |
| `logical_transition_proof` | `trusted / uncertain / insufficient` | before→after 是否形成可信逻辑转换 | 代替页型识别或直接授权规则匹配 |
| `target_trust` | 结构化判定 | 当前动作目标是否可复验、唯一且可安全命中 | 代替页面转换验效 |

`visual_target_confidence` 是 `target_trust` 内部的视觉目标评分，不是第五个可相互替代的信任域。它只回答候选区域是否足以进入受限坐标能力：

| 目标类别 | 最低 `visual_target_confidence` | 必需证据 |
| --- | ---: | --- |
| 底部主导航 | `0.85` | 导航带布局、目标槽唯一、OCR / 视觉标签、未选中状态 |
| 内容或视频卡片 | `0.90` | 卡片边界唯一、非广告、内容特征和详情页导航意图 |
| Profile 设置、签到或支付入口 | `0.90` | 来源已确认是 `profile`、精确标签、区域唯一且位置合理 |

模型单独输出高分仍不能签发坐标能力；候选必须同时通过类别白名单、风险词过滤、遮挡检查、区域唯一性和来源拓扑。视觉目标评分不得复制给 `page_type_confidence`。点击后的页型只能由新的 after Capture 重新识别；多证据视觉融合可以形成高于弱模型上限的确定性页型结果，但必须声明独立证据来源和完整页型结构契约。

具体约束：

- 未匹配页型的弱确定性修正继续保持现有 `min(model_confidence, 0.5)` 上限，不改成 `0.7`；
- 只有满足完整页型结构契约的强确定性识别才允许使用 `confidence_source=deterministic_structure`，此时可得到高于 `0.7` 的页型置信度；
- `render_profile_confidence` 来源于渲染器强证据，不受模型页型置信度上限影响，但不得反向抬高 `page_type_confidence`；
- `logical_transition_proof=trusted` 只表示状态转换可信，不表示 after 页型已经可信；
- SPA / Flutter 页面计入覆盖并允许页型规则匹配，必须同时满足：逻辑转换可信、`detection_status=recognized`、`page_type_confidence >= 0.7`，并且页型结果绑定 after 的 `identity_fingerprint`；
- 逻辑转换可信但页型置信度不足时，保留并继续探索新的逻辑状态，但不增加页型覆盖、不执行页型规则；
- 页型置信度足够但逻辑转换证据不足时，不创建新的逻辑页面身份，进入同会话有界重观察；
- 普通多页 DOM H5 继续使用现有覆盖和规则门禁，不因缺少上述可选逻辑字段而降级。

## 9. 状态转换判定

每次 before→action→after 输出：

- `logical_page_changed`：到达新的逻辑页面，增加覆盖；
- `surface_state_changed`：同一逻辑页面的弹窗、加载或局部状态变化；
- `unchanged`：动作没有产生可信效果；
- `uncertain`：证据冲突，需要同会话有界重观察。

SPA / Flutter 判定 `logical_page_changed` 时，需要一个身份锚点变化和一个独立佐证：

1. 主导航激活项变化 + 主内容骨架变化；
2. 页面根或标题变化 + 主要控件组变化；
3. 实际 `page_type` 变化 + 独立结构证据变化；
4. Flutter 语义根或返回结构变化 + 主语义组变化。

对 `semantics_sparse` 的 Flutter / Hybrid，视觉转换允许替代上述语义锚点，但必须稳定两帧并满足对应合同：

- 底部导航：目标选中态变化 + 主体视觉区域持续变化；
- 内容卡片：主体布局变化 + 标题、返回入口、正文或视频播放区域等详情特征；
- Profile 专属入口：before 已确认 `profile` + after 出现 `settings`、`payment` 或 `activity` 对应视觉 / OCR 结构。

点击涟漪、单个按钮颜色变化、加载动画、Toast 或单帧差异不能形成 `logical_page_changed`。URL、history、DOM 或 Semantics 变化可以加强证据，但对视觉主导的 Flutter SPA 不是必需条件。

普通多页 H5 保持现有 document / URL 导航规则，不增加门槛。

URL 的处理原则：

- URL 不变不代表动作失败；
- SPA 的 URL、query 或 hash 改变只能作为辅助证据；
- 普通多页导航继续按现有 URL 和 document 证据处理；
- URL 改变但主内容身份未变时，不自动增加 SPA / Flutter 页面覆盖。

`expected_page_type` 只表达动作意图，不得覆盖 after Capture 的实际识别结果。

## 10. 统一动作目标

DOM SPA 与 Flutter 目标统一为内部契约：

```json
{
  "source": "dom",
  "semantic_target": "个人中心设置入口",
  "target_signature": "sha256:...",
  "label": "设置",
  "role": "button",
  "interactive": true,
  "disabled": false,
  "rect": {},
  "parent_signature": "sha256:...",
  "center_hits_target": true,
  "source_capture_id": "uuid",
  "source_state_fingerprint": "sha256:..."
}
```

受限视觉坐标目标使用独立能力，不伪装成 DOM / Semantics 目标：

```json
{
  "source": "visual",
  "capability": "visual_navigation_coordinate",
  "source_capture_id": "uuid",
  "source_artifact_id": "uuid",
  "source_artifact_sha256": "sha256:...",
  "source_identity_fingerprint": "sha256:...",
  "source_state_fingerprint": "sha256:...",
  "render_profile": "flutter_web",
  "semantics_coverage_level": "semantics_sparse",
  "target_kind": "primary_navigation",
  "label": "我的",
  "expected_page_type": "profile",
  "visual_target_confidence": 0.93,
  "image_rect": {},
  "normalized_rect": {},
  "viewport_metrics": {},
  "model_image_transform": {},
  "local_visual_digest": "phash:...",
  "surrounding_anchor_digest": "sha256:...",
  "target_signature": "sha256:..."
}
```

能力必须封存原始截图尺寸、`visualViewport` 尺寸与偏移、设备像素比、滚动位置，以及模型输入相对原图的缩放和裁剪参数。报告只能公开摘要，不能输出完整 OCR 文本、页面原图或能力签名。

执行原则：

- 普通 DOM 继续使用现有 CSS 或结构化 selector；
- DOM SPA 目标由标签、角色、父级结构、激活状态和几何共同验签；
- Flutter 使用稳定语义目标和内部 Runtime selector，不跨 Capture 保存动态节点 ID；
- Hybrid 页面合并 DOM 与 Semantics 候选，重叠目标按可信度去重；
- Runtime 点击前必须在实时页面重新解析和复验；
- OCR 文本和截图区域不得生成通用动作目标，只能在第 7.3 节白名单内生成 `visual_navigation_coordinate`。

### 10.1 视觉坐标合同

所有最终可执行坐标统一为浏览器视口 CSS 像素。Flutter `transform: scale(k)`、内联样式坐标、Semantics 局部坐标、模型缩放坐标和原始截图像素均不能直接交给浏览器。

换算链固定为：

```text
模型输入坐标
  → 逆裁剪 / 逆缩放得到原始截图像素
  → 映射到截图裁剪对应的 CSS 区域
  → 加入 visualViewport 偏移
  → 浏览器视口 CSS 坐标
```

能力同时保存原图像素矩形和 `0..1` 归一化矩形，用于审计和动作前重识别；归一化矩形不能在视口变化后直接缩放执行。最终点位必须位于目标矩形的安全内部区域、保留边缘间距、处于可见视口，并避开系统栏、滚动条、键盘和遮挡层。内容卡片矩形不能覆盖主要页面；底栏目标必须位于已识别的底部导航带。

## 11. 动作前安全门禁

动作执行前必须确认：

1. 来源 Capture 仍是当前页面；
2. 当前逻辑页面指纹与候选绑定值一致；
3. 目标唯一、可见、未禁用；
4. 中心没有被其他元素遮挡；
5. 当前没有未处理的高层弹窗；
6. 动作属于现有策略允许的低风险类型；
7. 页面拓扑允许该入口；
8. 来源 Capture、目标、动作和 Surface Session 归属一致。

`visual_navigation_coordinate` 还必须立即采集最新视口截图并确认：

1. 来源 `identity_fingerprint` 未改变，且没有新增弹窗、广告或 loading 遮挡；
2. 视口尺寸、偏移、DPR、滚动位置与能力一致；视口改变时不得按旧比例盲目缩放；
3. OCR / 视觉标签、局部图像特征和周边锚点仍能唯一匹配；
4. 目标仍属于原白名单类别，安全点仍位于可见且无遮挡的目标内部；
5. 能力签名、截图 Artifact 摘要和来源状态链完整。

如果只有位置变化且目标仍可唯一证明，Runtime 可以基于最新 Capture 重新识别并重新签发一次能力；标签、布局、遮挡、唯一性或来源身份变化时必须失败关闭。重新签发不是对旧坐标的静默修补，必须产生新的 Capture、Artifact、能力签名和审计记录。

Profile 子页面保持局部拓扑：

```text
profile
  ├─ activity / 签到
  ├─ settings
  └─ payment
```

只有来源逻辑页面为 `profile`、入口能力受信且 after 实际结构符合目标页型时，才增加相应覆盖。其他页面的同名文本不能获得该能力。

## 12. 操作后验效

固定流程：

```text
before Capture
  → 实时目标复验
  → 单次动作
  → 页面稳定等待
  → after Capture
  → 逻辑页面身份与表面状态比较
```

SPA / Flutter 页面在稳定等待后，逻辑身份至少连续两次观察一致，才用于最终页面转换判定，避免动画中间帧成为伪页面。

判定规则：

| after 事实 | 动作结论 |
| --- | --- |
| 身份指纹可信变化 | 导航成功并增加逻辑页面覆盖 |
| 身份不变、弹窗或加载等状态变化 | 同页状态动作成功，不增加覆盖 |
| 没有可信变化 | 动作无效果 |
| DOM、Semantics、视觉证据冲突 | `uncertain`，同会话有界重观察 |
| 实际页型与预期页型不同 | 保留真实页型并记录导航不匹配 |

视觉坐标动作增加以下收口：

- 实际页型置信度不低于 `0.7`、逻辑转换可信且结果绑定 after `identity_fingerprint` 时，才计入页型覆盖并执行规则；
- 转换可信但页型置信度不足时，保留新逻辑状态继续观察，不计页型覆盖；
- 实际页型与预期不一致时记录真实页型，原候选以 `arrival_page_type_mismatch` 关闭；
- 只出现弹窗时记录同一 identity 的 surface state，不增加页面数；
- 点击无效果时使用 `VISUAL_NAVIGATION_NO_EFFECT`，同一目标只允许一次无副作用重观察；
- 进入外链、下载或风险页面时停止业务点击，尝试安全返回并记录 `VISUAL_NAVIGATION_UNSAFE_DESTINATION`；
- `payment` 只验证页面进入，不生成或执行任何交易提交目标。

重观察期间不能重复点击旧目标。必须先生成新 Capture，并基于新状态重新签发候选。

现有 `transition_kind` 保持兼容；新增逻辑转换放入可选 `action_trace.state_diff.logical_surface_transition`，避免破坏已有消费者。

## 13. 截图与 OCR 降级

触发条件：

- Flutter Semantics 激活失败；
- Canvas 主体没有足够语义；
- DOM / Semantics 的可见文字覆盖不足；
- 页面识别需要视觉布局补充。

边界：

- 截图用于布局、主导航选中态、弹窗、全屏广告和视觉状态；
- OCR 用于补充可见文字和区域；
- 补证结果必须绑定当前 `page_capture_id`；
- 补证可以提高页面识别置信度；
- 截图或 OCR 不能授权通用坐标点击；
- 只有 `flutter_web/hybrid + semantics_sparse`、独立功能开关启用、目标命中白名单并通过密封能力与实时复验时，才允许受限 XY 点击；
- DOM、Semantics 充足的 Flutter / Hybrid 和普通 H5 不进入视觉坐标分支。

纯 Canvas 且没有可用 Semantics 时，可以保留页面类型证据；若没有通过视觉白名单、置信度、来源拓扑或动作前复验，则仍以 `interactive_exploration_blocked` 收口。视觉能力是受限业务导航例外，不得扩展为任意 OCR 文本、任意图标或任意坐标点击。

## 14. 失败与降级

- Render Profile 检测失败：继续现有 H5 路径；
- 单个 Evidence Adapter 失败：记录 `degraded_reasons`，其他适配器继续；
- Flutter Semantics 激活失败：Capture 不失败；
- 身份指纹证据不足：保留 `needs_more_observation`，不猜页型；
- 相同逻辑身份和状态重复出现：复用现有 visited / candidate 去重与预算收口；
- 证据冲突：不增加页面覆盖，不执行旧目标，进入有界重观察或人工复核；
- 可选 metadata 写入失败：不允许悄悄把 SPA / Flutter 结果伪装为已证明的逻辑页面。

视觉坐标分支使用固定错误码：

- `VISUAL_TARGET_CONFIDENCE_LOW`：视觉目标评分低于目标类别门槛；
- `VISUAL_TARGET_NOT_ALLOWLISTED`：目标类别、来源拓扑或风险语义不在白名单；
- `VISUAL_TARGET_NOT_UNIQUE`：实时页面存在零个或多个相同目标；
- `VISUAL_TARGET_SOURCE_STATE_STALE`：来源 identity、state、Artifact 或局部视觉事实已经漂移；
- `VISUAL_TARGET_COORDINATE_INVALID`：坐标变换不完整、越界或不在安全内部区域；
- `VISUAL_TARGET_OBSTRUCTED`：目标被弹窗、广告、键盘或其他活动层遮挡；
- `VISUAL_NAVIGATION_NO_EFFECT`：点击后只有瞬态变化或没有可信变化；
- `VISUAL_NAVIGATION_UNSAFE_DESTINATION`：动作进入外链、下载或风险页面；
- `VISUAL_TRANSITION_EVIDENCE_INSUFFICIENT`：after 状态稳定但不足以证明逻辑转换。

单个视觉候选失败只关闭该候选，不默认终止整个任务。每个逻辑状态只处理有限数量候选，每个候选最多重新定位一次；视觉调用、重观察和 XY 动作全部计入现有探索轮次、动作和时间预算。

## 15. 配置与发布

能力分阶段启用：

1. Shadow：只计算 Render Profile、两层指纹和转换结果；
2. 判定启用：SPA / Flutter 逻辑状态影响覆盖，但不改变动作候选；
3. 目标启用：可信 DOM SPA / Flutter Semantics 候选进入现有 Frontier；
4. 视觉补证：按需使用截图 / OCR 参与识别；
5. 视觉坐标：仅对语义贫瘠的 Flutter / Hybrid 启用受限视觉导航。

配置名称固定为：

- `ALIOTH_H5_LOGICAL_SURFACE_ENABLED`：总开关，同时作为快速关闭开关；
- `ALIOTH_H5_LOGICAL_SURFACE_SHADOW`：只写审计结果，不影响状态、候选和覆盖；
- `ALIOTH_H5_LOGICAL_TARGETS_ENABLED`：允许可信 SPA / Flutter 目标进入现有 Frontier。
- `ALIOTH_H5_VISUAL_COORDINATE_MODE`：`off|shadow|enforced`，默认 `off`。

视觉坐标模式含义固定为：

- `off`：不运行图片 / OCR 坐标候选，现有流程完全不变；
- `shadow`：识别、验签并记录候选，但不进入可执行 Frontier、不点击、不改变覆盖；
- `enforced`：仅 `render_profile in {flutter_web, hybrid}` 且 `semantics_coverage_level=semantics_sparse` 时允许坐标候选进入 Frontier。

总开关关闭后普通 H5 行为恢复为当前基线。目标开关和视觉坐标模式不能绕过总开关；逻辑 Surface Shadow 开启时，视觉坐标即使配置为 `enforced` 也只能执行 Shadow。DOM、Semantics 证据充足的页面继续走原有主链，不能因开启视觉坐标而改变候选或执行方式。

## 16. 报告与审计

报告在现有页面覆盖之外增加：

- `render_profile` 分布；
- 逻辑页面数量；
- 页面表面状态转换数量；
- 同 URL 逻辑页面转换；
- Flutter Semantics 激活与覆盖情况；
- 视觉 / OCR 降级次数；
- `interactive_exploration_blocked` 等阻断原因；
- 预期页型与实际页型不匹配记录。
- 视觉坐标模式、图片 / OCR 调用次数；
- 视觉候选、验签、点击、重定位和拒绝数量；
- 视觉动作的目标类别、预期 / 实际页型、before / after Capture ID 与转换结论；
- 视觉坐标带来的新增逻辑页面和可信页型覆盖，以及各视觉错误码数量。

报告不把弹窗、loading、筛选、分页和轮播计入业务页面覆盖，也不输出完整 OCR 文本、页面原图、账号信息或原始能力签名。

## 17. 测试设计

### 17.1 单元测试

- Render Profile 强信号、弱信号、冲突和稳定性；
- DOM 身份信号归一化；
- Flutter 动态节点 ID 去除；
- 列表数量分桶和几何量化；
- 页面身份与表面状态指纹分离；
- Hybrid DOM / Semantics 候选去重；
- Profile 局部拓扑门禁；
- 普通 OCR / 截图不得签发通用点击，只有完整视觉白名单合同可以签发 `visual_navigation_coordinate`；
- Flutter `scale=1/0.5`、DPR `1/2`、模型缩放和截图裁剪后的坐标统一换算为视口 CSS 像素；
- 底栏、内容卡片、视频卡片和 Profile 三类专属入口的视觉签发正例；
- 广告、外链、登录提交、支付提交、重复标签、全页大矩形和越界坐标反例；
- 截图过期、视口变化、弹窗遮挡、目标漂移和重新签发一次的预算；
- 页型、渲染类型、逻辑转换和目标可信度互不串升；
- 弱页型修正保持 `0.5` 上限，强确定性页型结构可独立超过 `0.7`；
- 可选 metadata 和旧协议兼容。

### 17.2 转换测试

- DOM SPA 同 URL 的 `home → profile`；
- DOM SPA hash / query 改变但主体不变；
- Flutter 同 URL 主导航切换；
- URL / history 不变但视觉选中态和主体区域稳定变化的 Flutter SPA 切换；
- 只有点击涟漪、Toast、按钮颜色或单帧动画变化时不得形成新页面；
- Flutter 动态语义节点 ID 重建但页面不变；
- 页面根变化但主内容未稳定时返回 `uncertain`；
- 弹窗打开 / 关闭只改变表面状态；
- loading、分页、筛选、轮播和滚动不增加页面数；
- 实际页型与预期页型不一致；
- 无效果点击与重复状态收口。

### 17.3 集成测试

- 普通多页 H5 的 Capture、候选顺序、`state_version` 和覆盖结果与当前基线一致；
- `ALIOTH_H5_VISUAL_COORDINATE_MODE=off` 时不运行视觉坐标候选，`shadow` 时不进入可执行 Frontier 且不改变覆盖；
- Hybrid 页面中的 HTML 输入框和 Flutter 导航分别由正确适配器解析；
- Flutter Semantics 激活失败时 Capture 成功且交互安全关闭；
- 语义贫瘠 Flutter / Hybrid 的密封能力可以完成单次 XY 点击，并由 after Capture 证明实际到达；
- 视觉候选失败只关闭当前候选，不把未知按钮或旧坐标降级为通用点击；
- before / action / after 的 Capture、Action Execution 和 `state_diff` 归属完整；
- Worker、证据交接、报告和现有完成门禁不因可选字段缺失而失败。

### 17.4 真实站点验证

- 选择普通多页 H5、DOM SPA、Flutter Web 和 Hybrid 代表站点；
- 对比视觉坐标 `off + shadow + enforced` 与现有探索输出；
- 验证同 URL 的逻辑页面覆盖；
- 验证 Profile 下 activity、settings、payment 的局部入口；
- 记录远端内容波动、登录态、入口可见性和任务预算造成的剩余风险。
- 单独报告视觉坐标带来的页面类型、规则覆盖、错误分布、耗时和模型用量增量。

## 18. 验收标准

1. 普通多页 H5 的现有回归测试保持通过，候选顺序和覆盖结果不发生非预期变化。
2. DOM SPA 和 Flutter Web 可以在 URL 不变时识别可信的新逻辑页面。
3. 弹窗、loading、分页、筛选、轮播和滚动不会增加业务页面覆盖。
4. Flutter 动态语义节点 ID 变化不会产生重复页面或失效目标。
5. 截图和 OCR 不能授权通用点击；受限视觉坐标必须同时满足 Flutter / Hybrid 语义贫瘠门禁、白名单、密封能力、实时复验和安全坐标合同。
6. 动作成功必须具有 before→action→after 和可信状态差异。
7. Profile 之外不能签发 activity、settings、payment 本地入口。
8. 不新增独立 Worker、公开 API 必填字段或数据库迁移。
9. 能通过开关停用新能力并恢复当前普通 H5 基线。
10. SPA / Flutter 只有在逻辑转换、页型置信度和身份绑定联合通过时才增加覆盖；任一信任域不能替代其他信任域。
11. 视觉坐标在 `off` 和 `shadow` 下不会改变普通 H5、DOM SPA 或 Semantics 充足页面的候选、执行和覆盖结果。
12. 视觉主导的 Flutter SPA 在 URL / history / DOM 均不变化时，可以通过持续两帧的选中态和主体区域变化证明逻辑转换。

## 19. 预期实现位置

实现计划应优先复用以下现有边界：

- `services/api/app/infrastructure/browser/page_capture.py`：采集 document 身份和同帧 UI 事实；
- `services/api/app/application/h5_exploration_observation.py`：逻辑状态向 Frontier 的投影；
- `services/api/app/agents/tools/page_detection_tools.py`：消费统一逻辑证据，不继续堆积渲染器专属执行逻辑；
- `services/api/app/infrastructure/browser/semantic_target_resolver.py`：统一目标的 Snapshot 验签；
- `services/api/app/infrastructure/browser/capture_runtime.py`：动作前后公共状态摘要；
- 新增职责单一的逻辑状态领域模块和渲染证据适配器，避免继续扩大现有大文件；
- 新增职责单一的视觉导航检测、白名单策略、能力签发、坐标换算和转换验效模块；检测模块不能持有浏览器，Resolver 不能自行猜测页型；
- 对应 H5 Capture、页面识别、Frontier、Runtime、Working Memory 和 Workflow 测试模块。

具体文件拆分、测试顺序和迁移步骤由后续实现计划确定；实现不得改变本设计已经确认的兼容与安全边界。
