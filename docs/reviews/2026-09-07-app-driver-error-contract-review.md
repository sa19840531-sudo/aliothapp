# App 驱动受控错误传递修复

日期：2026-09-07。范围：观星九项任务中的统一页面操作执行流程。基线：`59fc9a3f`。

## 问题与修复

`AppInteractionExecutor` 原先把 Driver 抛出的异常全部改为 `APP_DRIVER_FAILED / retryable=true`，导致 Official 驱动的租约失效、上下文缺失和动作阻断等不可重试原因丢失。现有 Frontier 不会因此自动重放动作，但动作账本中的错误分类和重试属性不正确。

- 在现有 SurfaceDriver 领域合同中增加 `SurfaceDriverError`，只声明受控错误码和重试属性。应用层不反向依赖 Official 基础设施。
- `OfficialAppRuntimeError` 继承该合同，保留原异常类型、构造方式和其他调用方行为。
- App 执行器仅对该受控类型保留错误码和重试属性；异常正文、堆栈和第三方附带字段不进入执行结果。
- 未知异常仍使用原通用错误；带同名属性的陌生异常不被信任。取消仍向外传播，不增加动作调用或重试。

## 验证

新增正式回归在旧生产代码上为 **4 failed、2 passed**：四种受控错误均丢失分类，未知异常脱敏与取消传播对照通过。修复后新增用例 **6 passed**；Executor、Official Runtime/API、上下文、Surface Adapter、Coordinator、正式恢复及组合根九文件合计 **228 passed**。

暂存区独立导出 `/tmp/alioth-driver-errors-index-D6r2Nv`，验证树 `c9441fb6f75c624473568771d97fd16644b31cac`（此后仅补充本文结果）。核对模块实际从导出目录加载，同一九文件组合 **228 passed，27.99 秒**。在导出目录临时移除受控捕获分支，新增测试重新得到 **4 failed、2 passed**；恢复后 **6 passed**，证明回归能拦住旧行为。工作区未实施故障注入。

交叉审查确认领域依赖、原异常构造合同、未知异常脱敏、取消传播与无重放边界，无新增阻断项。五个代码/测试文件 Ruff、暂存差异检查通过；本批七个白名单文件不含后续调度测试或无关工作区改动。

## 交付边界

无数据库迁移，无前端菜单或路由同步。本批仅本地提交，不推送、不部署；不修改局部候选优先级、Probe、新会话回放或 Flutter 遮挡。真实 Android、星语服务和正式任务 Completion Gate 不属于本地回归通过的证明范围。
