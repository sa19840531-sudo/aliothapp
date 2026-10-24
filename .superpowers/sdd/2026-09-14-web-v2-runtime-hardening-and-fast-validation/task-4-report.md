# Task 4：底层禁止广告点击

## 范围与结论

- 基线：`7926ba00`；工作树：`D:/project/Python/alioth/.worktrees/web-v2-dynamic-exploration-evidence`。
- 已读取根 AGENTS、当前设计全文、Task 4 计划与 brief、progress 全部裁决，以及相关应用/浏览器实现与测试。
- 请求校验、Playwright 私有入口、动作 dispatcher、runtime executor 和 request builder 拒绝显式广告 click、selector、坐标及 CLI/JS 回退；唯一成功广告命令为 `DIRECT_AD_OUTBOUND`。
- 广告独立上下文使用安全传输后的 href goto，只交付不持有浏览器对象的不可变证据。广告内容不再进入普通 DOM 分类或充分性分类；来源、成对截图、父 Capture、恢复和关闭证明继续保留。
- 没有数据库迁移、前端菜单/路由变化，也没有修改 App/H5 专属生产文件。用户未跟踪 plans/specs/outputs 均未修改。

## TDD 记录

Python 命令均使用 `D:/project/Python/alioth/.venv/Scripts/python.exe -m pytest`，工作目录均为上述工作树。

1. 初始 RED：`services/api/tests/test_web_ad_landing_evidence.py services/api/tests/test_web_capture_runtime.py -q` → **14 failed, 304 passed, 5 skipped**。真实缺口包括 request 接受 click、dispatcher 落入 selector/coordinate/CLI、裸 Page 动作能力暴露、危险 scheme 原因丢失及 runtime 成功执行旧广告 click。
2. RED：`services/api/tests/test_web_ad_landing_evidence.py -q -k 'smuggle or content_classification'` → **3 failed**，证明 direct 动作可携带点击目标且静态广告仍被写成不足。
3. RED：同文件 `-k 'closes_without or source_capture'` → **3 failed, 4 passed**，证明 close 原因丢失、无 Capture 候选仍可选择及适配器缺来源门禁。
4. RED：同文件 `-k 'upgrade_legacy or implicit_legacy or before_page_setup'` → **3 failed**，证明旧不足状态被升级、Web 隐式旧广告窗口可点击、私有采集入口缺提前门禁。
5. request builder 新测试初次被领域 fixture 校验阻止；补齐合法旧广告参数后重新运行 `services/api/tests/test_web_capture_runtime.py -q -k 'request_builder_cannot'` → **1 failed（DID NOT RAISE）**，再加入生产门禁。
6. 第一轮 GREEN（六个广告/辅助/runtime 文件）：**432 passed, 8 skipped**。
7. 扩展 GREEN（下列十个文件）：**639 passed, 8 skipped, 2 warnings in 19.23s**。
8. 提交前 fresh 精确 GREEN：`services/api/tests/test_web_ad_landing_evidence.py services/api/tests/test_web_auxiliary_navigation.py services/api/tests/test_web_capture_runtime.py -q` → **356 passed, 5 skipped, 2 warnings in 4.76s**。
9. `python -m ruff check` 覆盖本次四个生产 Python 文件及五个测试文件 → **All checks passed**；`git diff --check` → **exit 0**，只有仓库既有 LF→CRLF 提示。

扩展命令：

```text
D:/project/Python/alioth/.venv/Scripts/python.exe -m pytest services/api/tests/test_web_ad_landing_evidence.py services/api/tests/test_web_auxiliary_navigation.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_ad_landing_evidence.py services/api/tests/test_web_ad_evidence_integrity.py services/api/tests/test_web_ad_outbound_security.py services/api/tests/test_web_agent_page_capture.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_app_package_boundary.py services/api/tests/test_app_formal_restoration_coordinator_boundary.py -q
```

真实 Chrome 补证：PowerShell 设置 `ALIOTH_TEST_CHROME_EXECUTABLE=C:/Program Files/Google/Chrome/Application/chrome.exe` 后执行 `services/api/tests/test_web_ad_outbound_security.py::test_real_chromium_uses_only_safe_transport_and_isolates_source -q` → **1 passed**。证明独立 context、源 cookie 不共享、脚本禁用、安全传输和页面回收。

## 合同覆盖

| 合同 | covering tests |
|---|---|
| 请求只允许 direct；拒绝 click/goto/hover/evaluate/无动作 | `test_page_capture_request_rejects_every_non_direct_ad_action` |
| selector/coordinate/CLI 在动作执行前被拒绝 | `test_page_capture_dispatcher_rejects_ad_click_before_any_driver_path`、`test_direct_ad_request_cannot_smuggle_click_targets` |
| 私有页面采集及 Web 隐式广告窗口不可绕过 | `test_page_capture_impl_rejects_before_page_setup`、`test_web_dispatcher_rejects_implicit_legacy_ad_click` |
| runtime 不调用驱动，不生成旧 click request 或广告帧 | `test_ad_outbound_intent_rejects_legacy_click_before_browser`（CLICK/TAP）、`test_browser_request_builder_cannot_generate_legacy_ad_click`、`test_legacy_ad_click_never_persists_landing_sequence` |
| 独立 href goto、不可调用 locator/mouse/evaluate/context、源页不变 | `test_isolated_ad_href_is_readonly_and_closes_without_touching_source`、真实 Chrome 安全传输测试 |
| 导航/采证/关闭失败都回收 | 上述隔离测试的 goto/content/close 参数；`test_capture_adapter_consumes_failed_open_before_worker_rebuild` |
| 安全 HTTP(S)、DNS/跳转/跨域隔离 | `test_ad_adapter_rejects_unsafe_href_before_browser`、完整 `test_web_ad_outbound_security.py` |
| 来源 Capture/请求 URL/最终 URL/父 Capture/恢复/关闭证据 | `test_ad_candidate_requires_source_capture_lineage`、`test_ad_adapter_rejects_missing_source_capture_before_browser`、`test_capture_adapter_persists_direct_new_page_artifacts_under_source_capture`、`test_capture_adapter_persists_artifacts_before_reporting_return_failure`、`test_source_identity_drift_blocks_reuse` |
| 广告不普通分类；不升级历史不足状态 | `test_direct_ad_open_records_type_without_content_classification`、`test_static_script_shell_is_saved_without_running_javascript`、`test_runtime_does_not_upgrade_legacy_insufficient_ad_observation` |
| 历史 click 只读，无命令/gap | `test_legacy_ad_click_is_read_only_and_never_replayed`；已有 legacy audit 系列 |
| H5 普通点击/弹窗以及 App 边界 | `test_web_capture_runtime.py`、`test_ad_landing_evidence.py`、`test_h5_runtime_composition.py`、两个 App boundary 文件 |

## 变更文件

- 应用：`services/api/app/application/web_ad_landing_evidence.py`
- 浏览器：`services/api/app/infrastructure/browser/ad_isolated_page.py`、`page_capture.py`、`capture_runtime.py`
- 测试：`services/api/tests/test_web_ad_landing_evidence.py`、`test_web_capture_runtime.py`、`test_ad_landing_evidence.py`、`test_web_ad_evidence_integrity.py`、`test_web_ad_outbound_security.py`
- 规则：`docs/专项能力与高频回归规则.md`
- 本报告。

## 顾虑与未覆盖风险

- 更广调用额外包括 `test_page_capture_tool.py` 时结果为 **54 failed, 922 passed, 43 skipped**。单独以 `-x` 复现首失败明确是缺少默认 Playwright `chromium_headless_shell-1234/chrome-headless-shell-win64/chrome-headless-shell.exe`；未声称该文件的全部真实浏览器回归通过。改用已安装 Chrome 的广告隔离集成测试已通过。
- 八个常规 skip 涉及未提供的真实浏览器/数据库条件；最终 PostgreSQL/完整浏览器验收仍由 Tasks 11/12 完成。两个 warnings 为已知 Agent Framework experimental warnings。
- 父任务明确裁决：`capture_ad_landing_evidence=True` 无条件只能 direct，因此 H5 显式广告 click 旧断言迁移为拒绝；普通 H5 点击/弹窗等不变。这是显式广告合同迁移，不能宣称该旧行为仍受支持。
- `AdPageEvidenceView` 无 click/context 等同名 stub，也不持有底层 Page。访问任意未提供能力时由 `__getattr__` 抛结构化 `AdOutboundDenied(WEB_AD_CLICK_FORBIDDEN)`；因此 `hasattr` 对这些属性也会抛该业务异常。调用者应仅使用已列出的 immutable evidence API，不以反射检测动作能力。
- 广告继续使用禁脚本的静态呈现，可能缺少依赖脚本的可见内容；保存真实静态 DOM/PNG，不伪造脚本执行或业务分类。

## Fix round 1：两项 Important 门禁修复

基线 `e9fc10d4`。仅修改共用广告 validator 的模式条件，以及动态 external href 识别后重新调用该 validator；不引入新的生产接口。

| Finding | covering test | RED 证据 | GREEN 证据 |
|---|---|---|---|
| 私有采集接受 GOTO/OBSERVE + DIRECT_AD_OUTBOUND 组合 | `test_private_ad_capture_rejects_direct_action_in_non_trigger_mode[goto/observe]` | 页面 setup、goto/settle 已执行，错误码不是 `WEB_AD_CLICK_FORBIDDEN` | 共用 validator 要求 `mode=SINGLE_TRIGGER`，两例均通过且 Page 操作列表为空 |
| 动态 external href 识别后先预抓帧才拒绝 | `test_discovered_external_web_click_is_rejected_before_source_frame` | 请求初始两个广告标记均为 false，真实 locator href 探测后 frame=1、dispatcher=1 | 探测次数=1，frame=0、dispatcher=0、click=0；立即得到 `WEB_AD_CLICK_FORBIDDEN` |

RED/GREEN 精确命令：

```text
D:/project/Python/alioth/.venv/Scripts/python.exe -m pytest services/api/tests/test_web_ad_landing_evidence.py -q -k 'private_ad_capture_rejects_direct_action or discovered_external_web_click'
```

结果：**3 failed → 3 passed**，失败原因与上述两项评审复现一致。

本轮验证：

- 原 brief 精确三文件命令 → **359 passed, 5 skipped, 2 warnings in 4.97s**。
- 上文十文件扩展命令 → **642 passed, 8 skipped, 2 warnings in 18.52s**。
- 真实 Chrome 同一广告安全测试本轮首次失败：测试用 `browser.contexts[1]` 假定隔离上下文顺序，取到了源上下文。读取本地 Playwright `_browser.py` 确认 `_contexts` 是 `Set`，`contexts` 返回该集合的列表；修正测试按 `context is not source` 识别实例，生产隔离逻辑未改。随后复跑 → **1 passed, 2 warnings in 1.51s**。
- `python -m ruff check` 检查本轮修改的生产与测试文件，`git diff --check` 检查补丁；均通过。
- 此轮不重跑已知缺默认 Chromium 的完整 `test_page_capture_tool.py`，此前 54 个失败的限制保持单独披露；真实 Chrome 单例不能代表该套通过。

本轮变更：`page_capture.py`、`test_web_ad_landing_evidence.py`、真实 Chrome 测试的上下文身份选择一行、专项规则和本报告。无数据库迁移、无前端菜单/路由变化，用户未跟踪文件未触碰。
