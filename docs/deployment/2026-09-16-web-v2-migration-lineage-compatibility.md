# Web V2 合并 dev_2.0 的迁移兼容

远程 dev_2.0 将原有七个 Web 迁移重编号到 `20260914_0113`–`20260914_0119`，并在前面加入六项规则集、补采、H5 门禁与规则测试迁移。原 checkpoint/error audit 迁移顺延为 `20260916_0120`、`20260916_0121`。

## 升级路径

- 新库或远程 dev 原链：正常执行 `alembic -c services/api/migrations/alembic.ini upgrade head`。
- 旧专用 Web 库已到 `20260915_0115`：先使用 `scripts/repair_web_v2_migration_lineage.py` 审计。禁止直接 stamp，也不能重跑已存在的 Web 迁移。
- 未知版本、多头、Web 结构缺失或新增迁移部分落库：脚本拒绝修复，必须单独审查。

## 旧库维护窗口

1. 备份数据库，记录备份位置；确认没有运行中的任务，停止所有 API、Worker 和其他数据库写入者。
2. 显式设置目标 `ALIOTH_DATABASE_URL`；在项目根目录运行 `python -m scripts.repair_web_v2_migration_lineage`，确认 `repair_required`。脚本不读取隐式默认库。
3. 运行 `python -m scripts.repair_web_v2_migration_lineage --apply --writers-stopped`。
4. 脚本持有 advisory/版本锁，在同一事务内调用原六项迁移，检查新增结构及 Web 证据逐行摘要不变后，CAS 更新版本到 `20260916_0121`；失败整事务回滚。审计输出包含执行迁移文件 SHA256，不输出连接凭据。
5. 再次审计应为 `already_current`；正常 `upgrade head` 应无新增操作。启动新代码服务后验证就绪与探索。

`--writers-stopped` 是维护者的明确确认，不是自动停止服务。不可在仍有写入方时传入。六项迁移包含规则归属变更，故不可仅停止 Web worker 而遗漏共用此数据库的 API/其他 worker。

## 回归要求

必须覆盖：新库全链升级、旧链兼容、新旧结构等价、真实 Capture/checkpoint 保留、第四步异常整事务回滚、重复调用不再执行 DDL/DML，以及未知/半迁移状态拒绝。

结构基线由独立新库完整迁移生成，覆盖 Web/Capture 列类型与默认值、PK/FK/check 约束及验证状态、索引定义、触发器定义与启停状态、Web 函数签名与完整定义。只支持已审查的 public schema；摘要差异必须人工审查，不可忽略或自动重签。缺 claim_token、缺 claim 约束、缺租约索引、同名恒真验签函数均须拒绝升级。
