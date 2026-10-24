# CLI 点击原节点绑定补强审查

范围：修复复审发现的 CLI 节点绑定竞态。原实现虽然持有 ElementHandle，却通过可变化的 CSS 路径重新查询 backendNodeId；此时插入同文案、同属性的新节点，可能让最终核验误认该节点。

## 修复与边界

- backendNodeId 改从原 ElementHandle 的实际对象引用取得，CSS 仅用于 CLI 派发前的最后同节点核验；原有语义摘要、风险、同源、动作前后取证与禁止重放规则保持不变。
- 每个 document 安装一个不可配置、不可写的冻结闭包桥接，平台缓存原始 JSHandle；临时节点引用只接受私有 capability 写入。随机属性名只防冲突，不是安全边界；页面不能通过重定义属性或方法替换操作目标。
- 每次绑定后清空节点引用，释放 CDP 对象；导航后验证缓存上下文并重建桥接。页面抢占旧名称时拒绝，不调用页面自建的 probe，也不向它传递 capability。关闭会话释放缓存句柄。
- 不给 DOM 添加属性，不删除或隐藏节点，不增加 Agent 可执行 JS/Shell，不修改 CLI 协议、锁版或补丁。CLI 仍默认关闭。

## 红绿验证

同语义竞态用例在操作前准备完整文案和事件，竞态窗口只插入克隆节点，不改变摘要字段。旧实现的 `DOM.getDocument` 后与新实现的 `Runtime.evaluate` 前均触发相同注入；在 HEAD 隔离导出上，旧实现确实误点新节点并使测试失败，新实现拒绝且点击次数为零。

新增真实浏览器回归还覆盖不可重定义桥接、动作后清空引用、导航重建和同名伪桥接不被调用。

暂存区独立导出 `/tmp/alioth-cli-binding-index-gOkcyv`，代码验证树 `672373a0516310d1bc43d46f8c4c8dfe1c9902a0`（随后仅更新本文）：CLI/Runtime Holder 真实 Chromium **62 passed，71.19 秒，无跳过**；首次使用工作区相同锁版 CLI 依赖。导出目录先因缺失 node_modules 无法运行 Node 测试，随后离线 `npm ci --ignore-scripts` 完成干净安装并应用摘要匹配补丁，Node 补丁测试 **3 passed**；导出内新安装依赖的两个新增真实用例另为 **2 passed，17.39 秒**。Python Ruff 与暂存差异检查通过。

本批无数据库迁移，无前端菜单/路由变更，没有推送、部署或扩大正式 CLI 开关。localhost Chromium 样本不代替真实业务任务、Android 设备或 Completion Gate 验收；既有 7 项触屏/Flutter 基线失败继续单独保留。
