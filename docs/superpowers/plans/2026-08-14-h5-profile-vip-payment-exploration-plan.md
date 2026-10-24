# H5 Profile VIP Payment Exploration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 H5“我的”页以 DOM 或截图任一证据发现“立即开通/会员中心”，安全进入 `/vip`，并以 `/vip` 与“开通VIP”双信号确认会员支付页，同时继续阻断真实支付动作。

**Architecture:** 页面识别层扩展受控视觉矩形契约并按关键词优先级生成会员支付入口候选；H5 Observation 只把服务端验证过的视觉参数写入既有 `CandidateAction.metadata.action_parameters`；Web Runtime 对比原探索 Capture 与动作前 Capture 的可信 UI Snapshot，状态一致后才把全页比例坐标映射为 CSS 坐标执行一次点击。`/vip` 会员支付变体在页面识别收口处使用 URL 与文本双信号门，其他支付页维持原识别逻辑。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、Playwright、pytest、Ruff、现有 H5 Frontier / Interaction Runtime。

## Global Constraints

- 仅实现已确认的 H5“我的”页会员支付链路，不建设任意视觉元素通用点击能力。
- 入口优先级固定为“立即开通”高于“会员中心”；同一词下 DOM 高于截图坐标。
- DOM、截图/OCR 任一来源命中即可；视觉坐标最低置信度固定为 `0.80`，矩形面积不得超过截图 `35%`。
- 只有 pathname 为 `/vip` 且 DOM、可见文本、OCR 或模型截图证据任一来源出现“开通VIP”时，才确认会员支付页。
- “我的”页 DOM 入口为低风险，视觉坐标入口为中风险；支付页中的“立即开通/确认开通/立即支付/提交订单”必须为高风险。
- 视觉动作必须绑定原 `page_capture_id`、原 URL 和截图编号；动作前 Capture 的 URL、稳定页面状态、可见元素几何结构签名与可信主截图摘要必须与原探索快照一致，坐标无效、过期或被篡改时拒绝执行。
- 失败与返回复用现有 H5 Frontier restoration；相同候选由现有候选 ID 与幂等执行链保证不重复。
- 所有新增注释使用中文，文件保持 UTF-8。
- 不引入数据库迁移，不修改前端菜单或路由，不新增第三方依赖。

---

### Task 1: 页面目录、视觉模型契约与“我的”页入口仲裁

**Files:**
- Modify: `services/api/app/domain/page/page_type_catalog.json` payment 条目
- Modify: `services/api/app/agents/skills/phase/page-detection/SKILL.md`
- Modify: `services/api/app/agents/tools/page_detection_tools.py:290-350, 740-810, 1580-1660, 1988-2010, 2540-2790`
- Test: `services/api/tests/test_page_type_catalog.py`
- Test: `services/api/tests/test_page_detection_tools.py`

**Interfaces:**
- Produces: `VisualBounds` Pydantic 模型，字段为 `screenshot_index: int`、`left_ratio/top_ratio/right_ratio/bottom_ratio/confidence: float`。
- Produces: `CandidateAction.visual_bounds: VisualBounds | None`。
- Produces: `_with_profile_payment_entry_navigation_candidates(...) -> list[CandidateAction]`。
- Consumes: 现有 `_SemanticEntryParser`、`_candidate_action_from_semantic_entry`、`_target_visible_in_dom`。

- [ ] **Step 1: 写页面目录与视觉契约失败测试**

在 `test_page_type_catalog.py` 增加行为断言：`payment` 的 profile 入口词精确为 `("立即开通", "会员中心")`，OCR/DOM 信号包含“开通VIP”，不配置全局 `vip` route prefix，风险动作包含“确认开通”“立即支付”“提交订单”。

在 `test_page_detection_tools.py` 增加：

```python
def test_visual_bounds_rejects_inverted_or_out_of_range_box() -> None:
    with pytest.raises(ValidationError):
        page_detection_tools.VisualBounds(
            screenshot_index=1,
            left_ratio=0.8,
            top_ratio=0.2,
            right_ratio=0.3,
            bottom_ratio=0.4,
            confidence=0.9,
        )


def test_profile_payment_entry_prefers_immediate_open_over_member_center() -> None:
    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<button data-testid="member-center">会员中心</button>'
            '<button data-testid="open-now">立即开通</button>'
        ],
        page_url="https://example.test/profile",
        image_count=1,
    )
    payment_actions = [item for item in actions if item.expected_next_page_type == "payment"]
    assert [item.target for item in payment_actions] == ['button[data-testid="open-now"]:visible']
    assert payment_actions[0].risk_level == "low"
```

再覆盖：只有“会员中心”时生成后备 DOM 候选；非 `profile` 不生成；模型提供“立即开通”有效 `visual_bounds` 而 DOM 只含“会员中心”时，只保留“立即开通”视觉候选；低置信度、面积超过 35%、索引超过 `image_count` 的视觉候选不进入结果。

- [ ] **Step 2: 运行测试确认 RED**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest services/api/tests/test_page_type_catalog.py services/api/tests/test_page_detection_tools.py -k 'payment or vip or visual_bounds or member_center or immediate_open' -q
```

Expected: 因 `VisualBounds` 和 `_with_profile_payment_entry_navigation_candidates` 尚不存在、目录缺少专项词而失败。

- [ ] **Step 3: 最小实现目录与模型契约**

在 `page_type_catalog.json` 中只修改 `payment`：

```json
"ocr_keywords": ["开通VIP", "支付", "价格", "充值", "购买"],
"dom_hints": ["开通VIP", "vip", "payment", "pay", "price", "recharge"],
"target_keywords": ["立即开通", "会员中心"],
"risk_actions": ["立即开通", "确认开通", "立即支付", "点击支付", "确认购买", "提交订单", "提交充值"],
"route_prefixes": []
```

在 `page_detection_tools.py` 新增严格 Pydantic 模型：

```python
class VisualBounds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screenshot_index: int = Field(ge=1)
    left_ratio: float = Field(ge=0, le=1)
    top_ratio: float = Field(ge=0, le=1)
    right_ratio: float = Field(ge=0, le=1)
    bottom_ratio: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_ordered_bounds(self) -> "VisualBounds":
        if self.left_ratio >= self.right_ratio or self.top_ratio >= self.bottom_ratio:
            raise ValueError("视觉矩形边界必须严格递增")
        return self
```

给页面识别 `CandidateAction` 增加可选 `visual_bounds`。多模态消息中的截图标签改成 `[截图来源索引 {index}/{count}，证据标签 {label}]`，并在系统规则中要求视觉候选的 `target` 只能精确返回“立即开通”或“会员中心”。

- [ ] **Step 4: 最小实现入口仲裁**

新增常量：

```python
_PROFILE_PAYMENT_ENTRY_PRIORITY = ("立即开通", "会员中心")
_PROFILE_PAYMENT_VISUAL_MIN_CONFIDENCE = 0.80
_PROFILE_PAYMENT_VISUAL_MAX_AREA_RATIO = 0.35
```

实现 `_with_profile_payment_entry_navigation_candidates`：

1. 只接受 `profile` + stable 状态且无可见弹窗。
2. 按关键词层级查找 DOM actionable element 与模型视觉候选。
3. 一旦某个较高优先级关键词存在任一证据，忽略较低关键词。
4. 同一关键词同时具备 DOM 和视觉证据时只保留 DOM；只有 DOM 缺失时才保留中风险视觉候选。
5. 视觉候选必须是 click、目标精确等于入口词、矩形有效、置信度达标、面积不超 35%、截图索引不超 `image_count`。
6. 只允许独立全页 PNG 形成视觉候选；时序/滚动帧失败关闭。控制候选的 `purpose`、`expected_next_page_type="payment"`、`navigation_depth=1` 和 `target_keywords`，并把索引对应的可信 `artifact_id/sha256` 注入候选；不信任模型 purpose 或证据身份。

把该函数接在 `_with_visible_business_navigation_candidates` 之前，并让 `_without_unexecutable_candidate_actions` 仅对通过上述条件的 profile 视觉候选豁免 DOM 文本检查。

- [ ] **Step 5: 运行测试确认 GREEN 并回归页面识别**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest services/api/tests/test_page_type_catalog.py services/api/tests/test_page_type_registry.py services/api/tests/test_page_detection_tools.py -q
```

Expected: 全部通过。

- [ ] **Step 6: 提交 Task 1**

```powershell
git add services/api/app/domain/page/page_type_catalog.json services/api/app/agents/skills/phase/page-detection/SKILL.md services/api/app/agents/tools/page_detection_tools.py services/api/tests/test_page_type_catalog.py services/api/tests/test_page_detection_tools.py
git commit -m "[AI编程] feat: 补齐H5会员支付入口识别" -m "涉及模块：页面目录与H5页面识别；数据库迁移：无；前端菜单/路由：无需；测试风险：真实多模态坐标精度待环境样本验证。"
```

---

### Task 2: H5 Observation 视觉动作参数与幂等身份

**Files:**
- Modify: `services/api/app/application/h5_exploration_observation.py:120-310`
- Test: `services/api/tests/test_h5_exploration_observation.py`
- Test: `services/api/tests/test_maf_explorer_agent.py`

**Interfaces:**
- Consumes: Task 1 输出的 `candidate_actions[].visual_bounds`、`page_capture_id`、`page_url`。
- Produces: `_profile_vip_visual_action_parameters(...) -> dict[str, object] | None`。
- Produces: `CandidateAction.metadata.action_parameters` 中受控的 `target_mode="visual_coordinate"` 参数。

- [ ] **Step 1: 写视觉候选适配失败测试**

在 `test_h5_exploration_observation.py` 构造 `page_type="profile"` 的 detection result：

```python
{
    "action": "click",
    "target": "立即开通",
    "target_keywords": ["profile_vip_payment", "evidence:screenshot"],
    "risk_level": "medium",
    "purpose": "探索我的页会员支付入口",
    "expected_next_page_type": "payment",
    "navigation_depth": 1,
    "visual_bounds": {
        "screenshot_index": 1,
        "left_ratio": 0.10,
        "top_ratio": 0.40,
        "right_ratio": 0.90,
        "bottom_ratio": 0.50,
        "confidence": 0.92,
    },
}
```

断言 Frontier candidate 为 medium，且 `metadata.action_parameters` 精确包含：

```python
{
    "target_mode": "visual_coordinate",
    "visual_entry_kind": "profile_vip_payment",
    "visual_source_capture_id": "capture-1",
    "visual_source_url": "https://example.test/profile",
    "screenshot_index": 1,
    "left_ratio": 0.10,
    "top_ratio": 0.40,
    "right_ratio": 0.90,
    "bottom_ratio": 0.50,
    "visual_confidence": 0.92,
}
```

另加篡改测试：目标为其他文本、风险不是 medium、来源页面不是 profile、矩形字段缺失时，不生成视觉 `action_parameters`，候选不得以裸坐标进入 Runtime。断言两个相同语义但不同视觉矩形生成不同 candidate ID。

- [ ] **Step 2: 运行测试确认 RED**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest services/api/tests/test_h5_exploration_observation.py services/api/tests/test_maf_explorer_agent.py -k 'visual or action_parameters' -q
```

Expected: 视觉参数尚未写入，candidate ID 未包含矩形而失败。

- [ ] **Step 3: 最小实现受控视觉参数投影**

给 `_candidate_actions` 增加 `source_capture_id`、`source_page_url`、`source_page_type` 参数。实现 `_profile_vip_visual_action_parameters`，重复校验关键词、medium 风险、矩形范围、面积、置信度与 source page type；只返回上述固定字段。

候选 ID hash 增加 `visual_action_parameters`，确保后备坐标变更会形成新身份；现有 `metadata.action_parameters` 继续作为唯一 Runtime 参数入口。普通 click、navigate、seek 和广告候选保持原行为。

- [ ] **Step 4: 运行测试确认 GREEN**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest services/api/tests/test_h5_exploration_observation.py services/api/tests/test_maf_explorer_agent.py -q
```

Expected: 全部通过。

- [ ] **Step 5: 提交 Task 2**

```powershell
git add services/api/app/application/h5_exploration_observation.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_maf_explorer_agent.py
git commit -m "[AI编程] feat: 绑定H5会员入口视觉动作" -m "涉及模块：H5 Observation/Explorer；数据库迁移：无；前端菜单/路由：无需；测试风险：无真实浏览器坐标执行，下一任务补Fake Page集成验证。"
```

---

### Task 3: Web Runtime 受限视觉坐标解析与单次点击

**Files:**
- Modify: `services/api/app/application/interaction_runtime.py:25-55`
- Modify: `services/api/app/infrastructure/browser/semantic_target_resolver.py:30-125`
- Modify: `services/api/app/infrastructure/browser/capture_runtime.py:610-675`
- Modify: `services/api/app/infrastructure/browser/page_capture.py:187-230, 2020-2130, 2325-2490`
- Test: `services/api/tests/test_web_capture_runtime.py`
- Test: `services/api/tests/test_page_capture_tool.py`

**Interfaces:**
- Consumes: Task 2 的受控 `action_parameters`。
- Produces: `ResolvedInteractionTarget` 的视觉矩形、截图编号、来源 URL 与视觉入口类型字段。
- Produces: `PageCaptureRequest` 的同名受信视觉字段。
- Produces: `_run_validated_visual_coordinate_click(...) -> None`。

- [ ] **Step 1: 写 Runtime 视觉目标解析失败测试**

在 `test_web_capture_runtime.py` 增加：

```python
async def test_visual_profile_payment_target_resolves_without_dom_selector() -> None:
    reader = StaticSnapshotReader({
        "url": "https://example.test/profile",
        "viewport": {"width": 390, "height": 844},
        "document": {"scroll_width": 390, "scroll_height": 1688},
        "elements": [],
    })
    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="立即开通",
            parameters={
                "target_mode": "visual_coordinate",
                "visual_entry_kind": "profile_vip_payment",
                "visual_source_capture_id": "capture-1",
                "visual_source_url": "https://example.test/profile",
                "screenshot_index": 1,
                "left_ratio": 0.1,
                "top_ratio": 0.4,
                "right_ratio": 0.9,
                "bottom_ratio": 0.5,
                "visual_confidence": 0.92,
            },
        )
    )
    assert target.selector is None
    assert target.click_x_ratio == 0.5
    assert target.click_y_ratio == 0.45
```

分别断言错误 capture ID、错误 source URL、非白名单语义、低置信度、异常面积、非 click 操作抛出稳定 `SemanticTargetResolutionError`。

- [ ] **Step 2: 运行 Resolver 测试确认 RED**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest services/api/tests/test_web_capture_runtime.py -k 'visual_profile_payment' -q
```

Expected: 现有 Resolver 尝试 DOM selector 并返回 `SEMANTIC_TARGET_NOT_FOUND`。

- [ ] **Step 3: 最小实现 Runtime 白名单解析**

扩展 `ResolvedInteractionTarget`，加入 `click_x_ratio`、`click_y_ratio`、四边矩形、`visual_confidence`、`visual_source_url`、`visual_entry_kind`、`screenshot_index` 可选字段。

在 `UiSnapshotSemanticTargetResolver.resolve` 中先识别 `target_mode == "visual_coordinate"`，仅允许：

- operation 为 click/tap；
- semantic target 归一化后为“立即开通”或“会员中心”；
- `visual_entry_kind == "profile_vip_payment"`；
- source capture ID 与请求一致；
- snapshot URL 与 source URL 去掉 fragment 后一致；
- 四边、面积、置信度和截图编号再次通过服务端校验。

其他 target mode 继续走现有唯一 DOM selector 解析。

- [ ] **Step 4: 写 PageCapture 坐标执行失败测试**

在 `test_page_capture_tool.py` 使用 Fake Page：`page.url` 为 profile，`evaluate` 对坐标映射脚本返回 `{x: 195, y: 422, document_x: 195, document_y: 760, scroll_x: 0, scroll_y: 338}`，`page.mouse.click` 记录参数。断言：

- `selector=None` 且视觉字段完整时允许 click。
- 实际只点击一次 `(195, 422)`。
- action trace 包含 `resolved_by="validated_visual_coordinate"`、截图编号、比例矩形、置信度、文档点与实际 viewport 点。
- 当前 page URL 与 `visual_source_url` 不同则抛 `VISUAL_SOURCE_PAGE_STALE`，mouse 未点击。
- selector 与视觉字段同时存在、视觉字段不完整、视觉字段用于 hover 时被 `_validate_request` 阻断。
- 单个新页 URL 与来源同源且 pathname 精确为 `/vip` 时，当前会话页跟随该 URL 后关闭新页；多新页、跨域页或非 `/vip` 页不跟随。

- [ ] **Step 5: 运行 PageCapture 测试确认 RED**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest services/api/tests/test_page_capture_tool.py -k 'visual_coordinate' -q
```

Expected: click 仍强制 selector，视觉字段不存在而失败。

- [ ] **Step 6: 最小实现坐标映射与点击**

扩展 `PageCaptureRequest` 与 `_build_browser_action_request`，使 click 请求在 selector 和视觉位置中严格二选一。新增 `_run_validated_visual_coordinate_click`：

1. 比较当前 page URL 与受信 source URL，fragment 不参与比较。
2. 用 `page.evaluate` 根据 `document.scroll_width/scroll_height` 计算全页文档点，把目标滚动到 viewport 中心并返回 viewport CSS 坐标。
3. 校验返回坐标在当前 viewport 内。
4. 调用 `page.mouse.click(x, y)` 一次。
5. 把紧凑视觉定位与点击结果写入现有 action trace；不记录图片字节或完整 DOM。

给视觉会员入口请求固定传递 `visual_expected_path="/vip"`。扩展现有 `_record_navigation_outcome`：仅当本次出现一个新页、该页与来源同源且 pathname 精确为 `/vip` 时，先让当前会话页 `goto` 该 URL，再关闭新页并继续正常采集；多新页、跨域页或非 `/vip` 页仍只记录并关闭。H5 Frontier restoration 继续负责失败后的来源页恢复，不新增专项恢复器。

- [ ] **Step 7: 运行 Runtime 与 PageCapture 回归确认 GREEN**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest services/api/tests/test_web_capture_runtime.py services/api/tests/test_page_capture_tool.py services/api/tests/test_web_agent_page_capture.py -q
```

Expected: 全部通过。

- [ ] **Step 8: 提交 Task 3**

```powershell
git add services/api/app/application/interaction_runtime.py services/api/app/infrastructure/browser/semantic_target_resolver.py services/api/app/infrastructure/browser/capture_runtime.py services/api/app/infrastructure/browser/page_capture.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_page_capture_tool.py
git commit -m "[AI编程] feat: 支持会员入口受限视觉点击" -m "涉及模块：Web Interaction Runtime/PageCapture；数据库迁移：无；前端菜单/路由：无需；测试风险：真实DPR与超长页面需Playwright环境样本复核。"
```

---

### Task 4: `/vip` 双信号确认与支付上下文风险阻断

**Files:**
- Modify: `services/api/app/agents/tools/page_detection_tools.py:1515-1715, 2139-2175, 3260-3620`
- Test: `services/api/tests/test_page_detection_tools.py`
- Test: `services/api/tests/test_page_type_registry.py`

**Interfaces:**
- Produces: `_vip_payment_confirmation(...) -> _VipPaymentConfirmation`。
- Changes: `_with_deterministic_high_risk_actions(candidate_actions, *, page_type)`。
- Consumes: DOM chunks、OCR chunks、`model_output.matched_catalog_signals` 和 page URL。

- [ ] **Step 1: 写双信号与上下文风险失败测试**

在 `test_page_detection_tools.py` 增加完整检测用例：

1. `https://example.test/vip?from=profile` + DOM “开通VIP” + 模型 ambiguous，结果为 recognized/payment。
2. `/vip` 无“开通VIP”，即使模型返回 recognized/payment，结果降为 ambiguous 且 `page_type is None`。
3. 非 `/vip` 只出现“开通VIP”，不触发会员支付专项确认。
4. `/vip` + 模型截图信号 `matched_catalog_signals.ocr_keywords=["开通VIP"]`，DOM 无该词时仍确认 payment。
5. 普通支付路由与既有 payment 模型识别不受 `/vip` 门影响。

增加风险测试：

```python
def test_immediate_open_is_high_risk_only_on_payment_page() -> None:
    action = page_detection_tools.CandidateAction(
        action="click",
        target="立即开通",
        risk_level="low",
        purpose="点击立即开通",
    )
    assert page_detection_tools._with_deterministic_high_risk_actions(
        [action], page_type="profile"
    )[0].risk_level == "low"
    assert page_detection_tools._with_deterministic_high_risk_actions(
        [action], page_type="payment"
    )[0].risk_level == "high"
```

同时覆盖“确认开通”“立即支付”“提交订单”始终为 high。

- [ ] **Step 2: 运行测试确认 RED**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest services/api/tests/test_page_detection_tools.py services/api/tests/test_page_type_registry.py -k 'vip or immediate_open or confirm_payment' -q
```

Expected: `/vip` 当前会被 route-only 提升，缺少双信号门；风险函数不接收 page type。

- [ ] **Step 3: 最小实现双信号收口**

新增冻结 dataclass：

```python
@dataclass(frozen=True)
class _VipPaymentConfirmation:
    is_vip_path: bool
    text_confirmed: bool
    text_source: str | None
```

`_vip_payment_confirmation` 使用 `urlsplit(page_url).path == "/vip"`；文本按 DOM 可见文本、OCR chunks、模型 `matched_catalog_signals.ocr_keywords/dom_hints` 顺序查找“开通VIP”。

在业务路由与语义 refine 之后收口：

- 不是 `/vip`：不改既有结果。
- `/vip` 且文本确认：强制 `page_type="payment"`、`detection_status="recognized"`，reason 和 matched signals 写明双信号来源。
- `/vip` 但文本未确认：`page_type=None`、`detection_status="ambiguous"`，不得因 route prefix 或模型自报绕过。

query/hash 不参与 pathname 判断。

- [ ] **Step 4: 最小实现上下文风险**

把 `_with_deterministic_high_risk_actions` 改为接收 `page_type`。全局高风险新增“确认开通”“立即支付”；只有 `page_type == "payment"` 时把“立即开通”加入 marker。更新内部调用和现有直接单元测试。

- [ ] **Step 5: 运行测试确认 GREEN 与普通支付回归**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest services/api/tests/test_page_detection_tools.py services/api/tests/test_page_type_registry.py services/api/tests/test_page_type_assessment_contract.py -q
```

Expected: 全部通过。

- [ ] **Step 6: 提交 Task 4**

```powershell
git add services/api/app/agents/tools/page_detection_tools.py services/api/tests/test_page_detection_tools.py services/api/tests/test_page_type_registry.py
git commit -m "[AI编程] fix: 收口VIP支付页双信号判定" -m "涉及模块：H5页面识别与风险分级；数据库迁移：无；前端菜单/路由：无需；测试风险：截图模型信号依赖真实多模态供应商稳定返回。"
```

---

### Task 5: 专项规则文档、全量相关回归与实现审查准备

**Files:**
- Modify: `docs/专项能力与高频回归规则.md`
- Review: `docs/superpowers/specs/2026-08-14-h5-profile-vip-payment-exploration-design.md`
- Review: all files changed since `origin/dev_1.5`

**Interfaces:**
- Consumes: Tasks 1-4 完整实现。
- Produces: 长期回归规则、可审查提交范围和验证证据。

- [ ] **Step 1: 更新专项回归规则**

在文档新增“H5 我的页会员支付探索”章节，明确：

- DOM/截图任一命中；
- “立即开通”高于“会员中心”；
- 视觉矩形 0.80/35% 门槛与 Capture/URL 绑定；
- `/vip` + “开通VIP”双信号；
- profile 入口与 payment 提交的上下文风险差异；
- 回归命令。

- [ ] **Step 2: 运行专项与架构回归**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest services/api/tests/test_page_type_catalog.py services/api/tests/test_page_type_registry.py services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_maf_explorer_agent.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_web_agent_page_capture.py services/api/tests/test_page_capture_tool.py -q
```

Expected: 全部通过，0 failures。

- [ ] **Step 3: 运行 Ruff**

Run:

```powershell
& '.venv\Scripts\python.exe' -m ruff check services/api/app/agents/tools/page_detection_tools.py services/api/app/application/h5_exploration_observation.py services/api/app/application/interaction_runtime.py services/api/app/infrastructure/browser/semantic_target_resolver.py services/api/app/infrastructure/browser/capture_runtime.py services/api/app/infrastructure/browser/page_capture.py services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_page_capture_tool.py
```

Expected: `All checks passed!`

- [ ] **Step 4: 检查差异与范围**

Run:

```powershell
git diff --check origin/dev_1.5...HEAD
git diff --stat origin/dev_1.5...HEAD
git status --short
```

Expected: 无空白错误；只包含设计、计划、专项代码、测试和专项规则文档；`.venv` 不出现在状态中。

- [ ] **Step 5: 提交文档与计划跟踪结果**

```powershell
git add docs/专项能力与高频回归规则.md docs/superpowers/plans/2026-08-14-h5-profile-vip-payment-exploration-plan.md
git commit -m "[AI编程] doc: 固化H5会员支付专项回归" -m "涉及模块：H5专项回归文档；数据库迁移：无；前端菜单/路由：无需；测试风险：真实站点视觉坐标仍需环境冒烟。"
```

- [ ] **Step 6: 请求独立代码审查**

按 `requesting-code-review` 使用 `origin/dev_1.5` 作为 BASE SHA、当前 HEAD 作为 HEAD SHA，请 reviewer 逐项核对设计、计划、风险边界、TDD 测试和非扩展范围。Critical/Important 问题必须按 `receiving-code-review` 验证并修复；每项修复先补失败测试，再改生产代码并重跑相关回归。

- [ ] **Step 7: 最终验证与分支收尾**

审查问题处理完后重新执行 Step 2、Step 3、Step 4；然后使用 `finishing-a-development-branch`。用户已明确选择：合并到 `dev_1.5`，在合并结果上重跑同一专项回归，并推送 `origin/dev_1.5`；禁止 force push。
