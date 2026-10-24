# App 取消动作收尾审查

范围：修复正式 App 动作被取消后可能遗留 `executing` 记录的问题；复用现有 ActionExecution 仓库和任务取消入口，不新增恢复通道或数据库迁移。

## 两层收尾

1. Coordinator 已派发动作后收到 `CancelledError`，在当前协程内最多等待 5 秒记录 `APP_ACTION_OUTCOME_UNKNOWN_DURING_CANCELLATION`，标明后态未采集、原因为工作流取消，然后继续抛出取消。取消不代表设备未执行，因此禁止补发动作或新采 After；再次取消会中断补偿，不留下脱离主协程的后台任务。
2. 正式 Runtime 的明确任务取消以及 Worker 取消对账入口调用现有 `fail_nonterminal_for_task`。即使 Work 清理失败也尝试 Action 清理；租约已过期或 Session 已 LOST 时仍能收束计划中、执行中和证据待完成的动作。异常回到上层；关闭前的尽力收尾只记 task/run/异常类型，再继续安全关闭，Worker 兜底错误不能吞掉。

交叉审查还补齐两个取消补偿细节：动作状态已提交而图重建失败时，后续终态清理即使更新 0 行也必须重建投影，避免旧 executing 留在图中；Worker 对账失败不记录原异常堆栈，只记 task/run/type，并保留“清理未完成”标记以继续重试。前者复用既有取消/孤儿终态清理接口，不增加表或第二套投影来源。

普通 deadline、预算结束或其他终态仍使用原 `cancel_exploration_work`，只取消 Work，不能误用整任务 Action 清理。正式 Builder 必须注入动作仓库；轻量测试替身的兼容不能成为正式运行静默跳过审计的路径。

## 验证重点

- 原缺陷的动作取消用例在旧 Coordinator 上失败：状态仍为 `executing`；修复后保存未知结果与缺 After 信息，再领同一动作仍不能重放。
- 验证执行前取消零动作零采集；租约/数据库写入失败不掩盖取消，不输出秘密；二次取消不产生后台补偿任务。
- 真实隔离 PostgreSQL：实际获取并过期租约、Session 转 LOST，经过 `AppV2Runtime.cancel_workflow` 和真实仓库收尾；planned/executing 变 failed，evidence_pending 变 evidence_failed，已有 completed/failed 与另一任务保持不变。无 After 的失败图边保持空目标、证明不足、不计到达、不可回放；没有前态的计划动作不伪造图边。
- Runtime Builder、取消顺序、Work 失败后仍清 Action、正常 deadline 不清 Action、安全日志均有独立断言。
- 最后两项故障验证先在旧行为导出上失败：图重建第二次调用被跳过，Work 异常 traceback 泄漏测试 canary；当前真实 PG 可在更新 0 行后重建旧图，Worker 的 Work/Action 两类异常均脱敏且保持待重试标记。

## 最终暂存验证

- 独立导出 `/tmp/alioth-app-cancel-final-index-hSmEbl`，代码验证树 `ee942b4c0942c53700cb683b7cca33ba5d961b4b`（随后仅更新文档）。44 文件跨批组合 **1594 passed、12 skipped，31.34 秒**。包括两端调度/工作流/组合根、共享协调/权限/恢复、取消/Worker、页面图领域/读写/进度/范围、真实隔离 PG、浏览器 Runtime 和秘密输入窗口。
- 12 个跳过均为未打开 `ALIOTH_CLI_INTEGRATION` 的真实浏览器案例；同批代码已在 CLI 独立导出显式运行，CLI/Holder **62 passed**，不是将跳过计为通过，也不与组合测试相加为去重总数。
- 取消直接相关六文件首轮 **249 passed**；补齐最后两个故障场景后，新增取消测试三文件共 15 项（含真实 PG 2 项）全部通过。这些与跨批存在重叠，不累加计数。
- 除 `worker.py` 外，本批 Python 文件按完整规则执行 Ruff 并通过；`worker.py` 的导入/行长检查通过，原有 UP047 现代语法提示未在本次扩展修改。暂存差异检查通过。

测试数据库仅使用 `127.0.0.1:51309` 的显式隔离库 `alioth_dev20_merge_20260907`，默认连接也指定到隔离库 `alioth_page_graph_scope_hardening_20260907`，不连接业务库。没有真实 Android/星语调用、正式 QC 或 Completion Gate 验收，没有前端菜单/路由变更，也没有推送。7 项既有触屏/Flutter 基线失败继续保留，不以本轮组合测试替代仓库全测。
