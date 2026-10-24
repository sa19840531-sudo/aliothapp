# H5 五站探索阻断修复实施计划

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复本次五站真实探索中已证实的开屏广告逻辑目标误判与页型覆盖错误，让可信“跳过”控件能够执行，并确保根路由业务修正规则不覆盖仍可见的 `startup_ad`。

**Architecture:** 保持现有“页面检测签发结构能力 → Runtime 读取受信 UI Snapshot → 逻辑目标唯一验签 → Playwright 执行”的分层。执行验签复用已登记的开屏结构合同，不回退到纯文本猜测；页型流水线保持生命周期页优先于底层业务路由。证据库存时序作为独立诊断项，在复跑后依据新的终态事实决定是否需要后续改动，不以 sleep 或放宽完成门禁掩盖。

**Tech Stack:** Python、FastAPI、pytest、Playwright、PostgreSQL、Docker Compose。

**Spec:** `docs/reports/2026-09-01-h5-five-site-clean-baseline-pre-fix.md` 与本次五站 UI Snapshot/Work/Action 数据库事实。

---

## Task 1：建立当前代码测试基线

- [x] 运行 `test_web_capture_runtime.py` 中现有 startup-ad resolver 用例。
- [x] 运行 `test_page_detection_tools.py` 中现有 startup-ad 与业务路由用例。
- [x] 记录任何与本次改动无关的既有失败；基线失败时先定位，不覆盖。

## Task 2：以真实三层 DOM 快照复现逻辑目标误判

- [x] 在 `services/api/tests/test_web_capture_runtime.py` 添加 countdown/jump_over 三层同文案用例；使用 `logical_targets_enabled=True` 与真实逻辑目标合同。
- [x] 添加 van-popup/skip-wrap 父子同文案用例，断言结构签发目标可绑定且保留审计矩形。
- [x] 保留至少一个重复结构控件的拒绝用例，证明修复不会放宽真正歧义。
- [x] 先运行新增测试并确认旧代码因 `LOGICAL_TARGET_NOT_UNIQUE` 失败。

## Task 3：最小化修复开屏结构目标验签

- [x] 在 `services/api/app/infrastructure/browser/semantic_target_resolver.py` 中让逻辑绑定优先使用已签发 selector 对应的开屏结构元素。
- [x] 仅对 `target_mode=startup_ad_dismissal`、click、已登记结构和中心 hit-test 成功的元素允许非原生交互容器通过；其他目标继续使用既有 `_operation_compatible` 门禁。
- [x] 保持 Runtime selector、target signature、expected rect 与 source state 审计字段不变。
- [x] 运行新增用例及全部 startup-ad resolver 相关测试。

## Task 4：以测试锁定生命周期页优先级

- [x] 在 `services/api/tests/test_page_detection_tools.py` 增加 `?mainTab=video + startup_ad` 路由矩阵用例。
- [x] 先确认旧代码错误返回 `home`。
- [x] 调整 `_refined_page_type_from_business_route` 判断顺序：仅业务页型进入 mainTab/root 路由纠正，`startup_ad`、`line_loading` 等生命周期页保持原值。
- [x] 运行路由矩阵、开屏识别和候选生成测试，确认 `startup_ad_dismissal` 候选仍被签发。

## Task 5：同步专项回归规则并完成静态验证

- [x] 更新 `docs/专项能力与高频回归规则.md`，记录“结构签发不得在逻辑绑定阶段退化为文本”和“mainTab 不覆盖生命周期页”。
- [ ] 运行相关测试文件、格式/静态检查；检查 Git diff 只包含计划、报告、测试、实现和规则文档。

## Task 6：无构建真实复跑五站

- [ ] 使用 `docker compose ... restart alioth-api alioth-worker`，禁止 build。
- [ ] 登录 admin 获取新 token，不输出凭据或 token。
- [ ] 创建五个全新任务并记录 task/run ID。
- [ ] 等待全部终态，持续核对 Worker heartbeat、动作、页面 Capture、Frontier、覆盖快照、证据库存与向量导航审计。
- [ ] 对比修复前后 `LOGICAL_TARGET_NOT_UNIQUE`、开屏关闭成功率、页面类型并集及 13 类用户目标覆盖。

## Task 7：处理复跑揭示的剩余阻断并交付最终报告

- [ ] 若仍出现代码级阻断，重复“真实证据 → 失败测试 → 最小修复 → 回归 → 无构建复跑”，不得凭推测扩大改动。
- [ ] 区分代码缺陷、模型/供应商不可用、站点内容变化、安全/认证/付费边界和目录产品决策。
- [ ] 更新最终报告，列出修复前后每站过程、页面、向量检索、Worker 效果、覆盖率、剩余阻断与未修复原因。
- [ ] 按 verification-before-completion 要求运行最终验证并核对工作区状态。
