# 第二轮截图审查整改设计

## 背景

本轮整改针对提交 `a60b9f5` 后的增量审查结果，范围包括部署默认值、远程 Host 鉴权、代理来源解析、证据并发写入、0038 历史数据兼容、Agent 完成门、Completion Gate 收口、Android 生命周期和任务洞察查询。

## 设计决策

1. **部署安全边界**
   - 启动脚本不再把示例环境文件作为运行时凭据来源。
   - Host Runner 启动必须加载显式本地配置，并拒绝缺失或占位 token；默认绑定回环地址，容器内部绑定与宿主端口映射分开处理。
   - 反向代理覆盖客户端提供的 X-Forwarded-For，应用只信任配置的代理网段，并解析代理追加的最后一个地址。

2. **远程控制与完成事实**
   - Host `/stop` 复用统一 HTTP 请求适配，强制携带 Bearer token。
   - 主站 `/stop` 的 `ok` 由本地进程处理结果和 Host 返回结果共同决定，不能在 Host 失败时返回成功。

3. **证据不可变写入**
   - Local Store 使用唯一临时文件加 `os.link` 的 create-if-absent 语义，避免 exists 与 replace 之间的竞态。
   - MinIO 使用 `If-None-Match: *` 条件 PUT；冲突后读取既有对象并严格比对 checksum、size、metadata 和签名，禁止无条件覆盖。
   - 补偿删除只删除本次成功创建的对象。

4. **0038 历史兼容**
   - Action Capture 先取得业务幂等键对应的 canonical Capture，再按 artifact type 解析既有 Artifact。
   - 历史 Artifact 身份事实一致时复用其 ID；事实不一致时失败并回滚；返回的 `CaptureRef` 始终使用 canonical ID。

5. **Agent 与 Completion Gate**
   - `done` 只作为请求，不得直接把运行状态改成 done；必须有已进入安全栏目、栏目动作/证据和明确安全栏目穷尽信号。
   - 危险点击的放行只接受结构化的 dismiss 控件角色，不扫描 reason、notes、标签等自由文本。
   - App QC 启用时 Worker 启动强制使用 PostgreSQL State Store 和 Task Finalization Service；失败停留 `session_draining`，由独立的持久化重试扫描器重新评估。

6. **Android 与洞察**
   - Mobile Demo Android Runtime 保存并释放真实 Session Gate，并使用真实 Evidence Exit Gate 依赖；关闭路径幂等。
   - Task Insight 的页面、规则和截图字段以 `qc_capture` 为公共事实源，App Capture 通过 metadata fallback，不再只依赖 `qc_page_capture`。

## 非目标

- 不调用业务远程 API，不在本地测试中伪造真实 PostgreSQL、MinIO、ADB 或官方 App API 的端到端验收。
- 不重写既有前端路由和菜单；本轮后端契约字段保持兼容。

## 验证策略

- 每个缺陷先补失败回归测试，再实现最小修复并运行对应测试。
- 合并前执行相关测试、全量后端测试、`compileall`、Ruff（仅检查已修改 Python 文件）和 `git diff --check`。
- 最终报告明确区分本地可验证项与未执行的真实基础设施验收。
