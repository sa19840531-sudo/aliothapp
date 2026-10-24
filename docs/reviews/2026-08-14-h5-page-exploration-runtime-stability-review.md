# H5 页面探索运行稳定性修复复审

## 复审范围

本轮仅复审审查截图中属于 H5 页面探索的两项问题：

1. `page_detection` 每轮发布证据需求集失败，包括失效归属、revision 落后和同 revision 内容漂移。
2. `page_capture -> page_detection -> 需求集发布失败 -> 继续采集` 形成循环并最终耗尽 `max_steps`。

Run 失败后的业务任务收口、Run API 实时进度不在本轮修改范围内。

## 复审结论

结论：通过，可合并。

- Critical：0
- Important：0
- Minor：0

需求集发布现在使用稳定 ID 保证同一 revision 幂等；并发 revision 冲突只重读、重算并重试最多三次，非冲突异常不会被误重试。仓储层仍在 capture 事务锁内复核最新 revision，避免应用层检查与写入之间产生竞态。

需求集发布失败现在以静态、脱敏、不可重试的页面级基础设施错误写入 WorkingMemory；无有效 Frontier 动作时立即以 `infrastructure_unavailable` 收敛，不再继续盲采集。Frontier 放行要求动作真实存在、状态为 candidate 或 selected、非高风险并满足来源页就绪约束，伪造、未知或已失败的动作 ID 均不能绕过熔断。

## 复审中发现并关闭的问题

首次独立复审发现：失败后的 Frontier 放行曾只检查 action ID 非空，伪造 ID 可以绕过页面级熔断。修复后增加真实可运行性校验，并覆盖 unknown、failed、真实 ready action 三类回归。

首次独立复审还指出缺少连续冲突耗尽边界测试。现已直接断言候选规则重算、latest 重读和 add 均恰好执行三次，随后重新抛出类型化 revision 冲突。

## 验证证据

- H5 页面探索核心回归：492 passed，2 个第三方实验性告警。
- Agent 运行时集成与并发回归：53 passed，2 个第三方实验性告警。
- Ruff：All checks passed。
- Python `compileall`：通过。
- `git diff --check`：通过。
- 独立代码复审：Critical、Important、Minor 均为 0，结论 Ready to merge。

## 剩余风险

真实 PostgreSQL 多 Worker 并发和 Playwright H5 长链路未在本地联合环境执行，发布前仍应在联合环境保留该验收门禁。本轮无数据库迁移，也不涉及前端菜单或路由同步。
