# 第二轮截图审查整改执行计划

> **执行说明：** 按本计划在隔离 worktree 内实现，完成后回到主 worktree 合并。

## 目标

修复第二轮截图审查列出的 P0/P1 和次要残余问题，建立可回归的安全边界、证据幂等、历史兼容和 Completion Gate 测试。

## 步骤

1. 建立部署脚本、远程停止、XFF 解析和基础配置的失败回归测试；修复脚本默认值、统一 HTTP 鉴权和代理解析。
2. 建立 Local/MinIO 并发写入与补偿删除测试；实现 create-if-absent 和 `created` 语义。
3. 建立 0038 历史 Capture/Artifact 冲突测试；实现 canonical Artifact 解析和冲突回滚。
4. 建立 Agent `done`、危险点击和 Worker Completion Gate 配置测试；实现严格完成证据、结构化 dismiss 和显式服务注入。
5. 建立 Completion Gate 失败后重试、Android close 释放 Gate、Task Insight App 字段回退测试；实现持久化重试扫描器和查询修复。
6. 运行分组回归与全量验证，审查 diff；提交 worktree 分支。
7. 在主分支保留用户改动的前提下拉取远程最新代码，合并整改分支，解决并验证冲突，commit 后 push。

## 提交要求

提交说明使用 `[AI编程]` 前缀，并说明涉及模块、数据库迁移情况、前端同步情况和未覆盖的真实环境风险。
