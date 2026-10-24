# H5 Profile 专属入口可信能力边界实施计划

> 执行要求：使用 systematic-debugging 的既有证据定位，按 test-driven-development 完成每项 RED/GREEN 循环，完成前使用 verification-before-completion 重新验证。

**目标：** 将 `activity`、`settings`、`payment` 收口为只从真实 `profile` Capture 和服务端受信结构产生的候选，验证动作落地页型，并补齐唯一全局启动广告跳过结构；随后无 build 复验全部 8 个 H5。

**架构：** 页面检测层签发专项能力，Observation 层在 Frontier 写入前二次校验，Working Memory 层验证实际落地页型。三个边界共同阻断模型幻觉、截图误点和动作 no-op。启动广告只新增可证明唯一的 DOM 结构，不扩大通用点击权限。

**技术栈：** Python 3.12、FastAPI、Pydantic v2、Playwright、PostgreSQL/pgvector、pytest、Ruff、Docker Compose。

## 全局约束

- `activity/settings/payment` 只能来自 `profile`。
- 三类候选必须带对应服务端专项签名，不允许通用视觉回退。
- 结果页型必须等于预期页型。
- 不修改向量阈值，不写站点域名特例。
- 不引入数据库迁移、前端路由/菜单或第三方依赖。
- 不覆盖用户在 `page_capture.py`、`test_page_capture_tool.py` 的现有未提交改动。
- 所有新增注释使用中文 UTF-8。

---

## Task 1：profile 专属候选生成边界

**文件：**

- 修改：`services/api/app/agents/tools/page_detection_tools.py`
- 测试：`services/api/tests/test_page_detection_tools.py`

- [ ] 写失败测试：非 profile 的 activity/settings/payment 候选全部被移除。
- [ ] 写失败测试：SVG `data-name` 或不可操作文本不产生 activity。
- [ ] 写保护测试：profile 上已有受信 activity/settings/payment DOM 结构仍产生候选。
- [ ] 运行定向测试并确认失败原因命中缺失边界，而非 fixture 错误。
- [ ] 最小实现统一 profile-local 候选过滤，删除 activity 可见文本回退。
- [ ] 重跑定向测试确认通过。

## Task 2：Observation Frontier 写入信任边界

**文件：**

- 修改：`services/api/app/application/h5_exploration_observation.py`
- 测试：`services/api/tests/test_h5_exploration_observation.py`

- [ ] 写失败测试：非 profile 来源的三类候选不进入 `candidate_actions`。
- [ ] 写失败测试：profile 页面上的通用视觉/语义候选无专项签名时不进入 Frontier。
- [ ] 写通过契约：三类受信专项动作参数被原样投影，其他页型候选不受影响。
- [ ] 运行 RED，记录失败输出。
- [ ] 最小实现来源页型与专项参数硬门，并调整参数选择顺序。
- [ ] 运行 GREEN。

## Task 3：落地页型业务验真

**文件：**

- 修改：`services/api/app/agents/runtime/page_inspection_memory.py`
- 测试：`services/api/tests/test_working_memory.py` 或现有对应 Working Memory 测试文件

- [ ] 参数化写失败测试：activity/settings/payment 专项动作的 actual 与 expected 不符时失败。
- [ ] 写保护测试：三类 actual 与 expected 一致时维持成功并计入 explored。
- [ ] 运行 RED。
- [ ] 将 settings 专项判断泛化为 profile-local 三类签名和页型校验。
- [ ] 运行 GREEN。

## Task 4：唯一全局启动广告跳过结构

**文件：**

- 修改：`services/api/app/agents/tools/page_detection_tools.py`
- 视实现需要修改：`services/api/app/infrastructure/browser/semantic_target_resolver.py`
- 视实现需要修改：`services/api/app/infrastructure/browser/page_capture.py`
- 测试：`services/api/tests/test_page_detection_tools.py`
- 测试：`services/api/tests/test_page_capture_tool.py`

- [ ] 写失败测试：startup_ad 中唯一全局 `.count-down` 精确“跳过”生成受信候选。
- [ ] 写失败测试：两个同名节点、非精确文本或非 startup_ad 不生成该候选。
- [ ] 写 Observation 测试：无受信关闭能力时保留倒计时重观察，不执行模型盲点。
- [ ] 运行 RED。
- [ ] 最小扩展结构解析和受控 selector；避免改动用户已有超时容错代码区段。
- [ ] 运行 GREEN 和 PageCapture 回归。

## Task 5：专项规则与静态验证

**文件：**

- 修改：`docs/专项能力与高频回归规则.md`

- [ ] 固化 profile-only、专项签名、落地页型验真和 startup_ad 唯一结构规则。
- [ ] 运行页面检测、Observation、Working Memory、PageCapture 定向测试。
- [ ] 运行相关 API 测试套件和 Ruff。
- [ ] 运行 `git diff --check`，核对用户已有改动未被覆盖。

## Task 6：无 build 容器复验

- [ ] 记录当前 compose 服务和镜像 ID。
- [ ] 只重启 API/worker（必要时连同 PostgreSQL/Redis），禁止 build。
- [ ] 自动登录 `admin`，令牌只保存在进程内且不输出。
- [ ] 对 8 个 H5 地址分别提交原始完整任务 payload。
- [ ] 持续监控到全部 main/judge 工作终态和队列耗尽。
- [ ] 提取每个任务的 Capture、页型、来源/落地关系、动作错误、Frontier 终态和向量解析统计。

## Task 7：修复验证报告

**文件：**

- 新增：`docs/2026-08-28-H5多站点完整探索修复验证报告.md`

- [ ] 逐站列出探索页面与页型。
- [ ] 区分 raw observed coverage 与有效 profile journey coverage。
- [ ] 对比 13 个用户目标页型及当前目录有效目标。
- [ ] 对比首轮和修复后的 worker、阻断、向量库应用效果。
- [ ] 明确仍受站点内容、登录态、不可见入口或任务预算影响的缺口。
- [ ] 最终核对报告可追溯到 task/run/capture/action 证据。
