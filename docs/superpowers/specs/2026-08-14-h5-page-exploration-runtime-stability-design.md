# H5 页面探索需求集发布与循环收敛设计

## 目标与范围

本次只修复 H5/Web 共用页面探索链路中与图片审查结果对应的两项问题：

1. `page_detection` 完成后发布 Evidence Exit RequirementSet 时，多个并发发布者可能基于同一旧 revision 计算下一版，导致合法竞争被误判为 revision 落后或同 revision 内容漂移。
2. RequirementSet 发布最终失败后，页面检测仍以 `recognized` 返回，Agent 会继续 `page_capture -> page_detection`，直到消耗 `max_steps`。

不处理任务终态收口和 Run API 实时进度；不修改 App 链路、前端菜单/路由或数据库结构。

## 根因

`EvidenceExitRequirementService.publish_for_capture` 在仓储事务之外读取 latest 并计算 `latest.revision + 1`。仓储虽然按 Capture 获取 PostgreSQL advisory transaction lock，但锁只覆盖校验和 INSERT；等待锁的并发请求仍携带锁外计算的旧 revision。另一个竞争细节是 `requirement_set_id` 不包含 `created_at`，而完整 payload 包含它：两个语义相同的 revision 可能具有同一稳定 ID、不同墙钟字段，现有全对象比较会把幂等重放误判为漂移。

`CaptureEvidenceReadinessService` 把发布异常压缩为 `failure_reason`，`_finalize_capture_evidence_readiness` 只记录日志且返回 `None`。因此 `persist_page_detection_for_current_run` 无法将控制面失败写回 Tool 结果，WorkingMemory 仍看到 `recognized`，页面探索也无法区分“页面识别成功”和“识别后置门禁事实未发布”。

## 设计

### 1. RequirementSet 发布并发闭合

- 在应用端口定义可识别的 `EvidenceExitRequirementRevisionConflict`；基础设施仓储只在“当前 revision 已被其他发布者推进”时抛出该冲突，归属无效、成员删除、终态回退等业务约束继续使用普通持久化错误且不得重试。
- 仓储取得 Capture 锁并加载 latest 后，如果待写快照与 latest 的稳定 `requirement_set_id` 相同，直接返回库内 latest 且 `created=false`。幂等判定不比较 `created_at`。
- 应用服务只对上述 revision 冲突做最多 3 次有界重算：每次重新读取 latest、重新合并成员并生成下一 revision；其他异常立即上抛。
- 不新增数据库迁移；现有 advisory lock、唯一键和追加式成员约束保持权威。

### 2. 发布失败显式进入页面探索状态机

- `CaptureEvidenceReadiness` 增加稳定 `failure_code` 与 `retryable`，异常详情只进入服务日志；Tool 返回使用静态错误消息，避免把数据库异常或敏感载荷暴露给 Agent。
- `_finalize_capture_evidence_readiness` 返回结构化 readiness。RequirementSet 发布最终失败时，`persist_page_detection_for_current_run` 将结果改为 `detection_status=error`、`rule_matching_allowed=false`、`retryable=false`，并携带 `EVIDENCE_REQUIREMENT_PUBLISH_FAILED` 与 `failure_kind=infrastructure`。
- WorkingMemory 使用该稳定错误码关闭当前页并保留具体失败事实；当前页不再停留在 DETECTING，也不会进入候选规则匹配。
- 失败后仍允许携带真实 `exploration_action_id` 的 Frontier 页面继续，避免一个页面故障吞掉其他已发现分支。
- 若模型忽略不可重试结果，继续对失败页调用 detection，或在没有 Frontier 身份时盲目 capture，则使用现有 `infrastructure_unavailable` 终止语义锁存 Run，禁止再次进入同一采集/识别循环并消耗 `max_steps`。

## 兼容性与审计

- 成功路径、Manifest pending 和 readiness 组合层未形成结果的路径保持原行为；只有服务明确返回的 RequirementSet 发布失败会改为页面错误。
- 失败错误码、页终态与 Run 终止原因进入 WorkingMemory 审计快照；模型可见 Tool 结果保留稳定错误码和不可重试标记。
- 无数据库迁移，无前端路由或菜单同步，无 App 行为变化。

## 验收

- 同一语义快照在锁内命中稳定 ID 时幂等返回库内对象，不执行第二次 INSERT。
- 一个模拟并发 revision 冲突会重新读取 latest 并成功发布下一连续 revision；非冲突错误只调用一次仓储。
- RequirementSet 发布失败后，page detection 返回结构化不可重试错误，当前页进入失败终态且不再规则匹配。
- 真实 Frontier capture 仍放行；对失败 capture 重复 detection 或无 Frontier 盲采会锁存 `infrastructure_unavailable`，不会走到 `max_steps_reached`。
- 相关 pytest、Ruff、compileall 与差异检查通过；真实 PostgreSQL/Playwright 多 Worker 竞争保留为联合环境验收风险。
