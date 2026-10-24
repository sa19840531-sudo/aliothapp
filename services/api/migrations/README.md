# migrations 数据库迁移

存放数据库结构变更的 Alembic 迁移脚本。

## 约定

- 每次涉及表结构变更（新增表、新增字段、索引调整）必须配套一个迁移脚本，禁止手工改线上库结构。
- 迁移脚本与 [../app/infrastructure/db/](../app/infrastructure/db/README.md) 下的 ORM 模型定义保持同步。
- 提交说明中需注明"是否引入数据库迁移"，遵循 [AGENTS.md](../../../AGENTS.md) 提交约定。
- 涉及历史数据迁移（如 `qc_finding` → `qc_rule_execution` 的演进）需单独评估迁移脚本与切换时机，参见[整合方案设计第 9 节](../../../docs/design/2026-07-03-alioth-integrated-solution-design.md)。

## 执行方式

数据库连接串不写入仓库，通过 `ALIOTH_DATABASE_URL` 注入；本地开发也可以写入项目根目录
`.env`，Alembic 会自动加载：

```bash
ALIOTH_DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/alioth' \
  .venv/bin/python -m alembic -c services/api/migrations/alembic.ini upgrade head
```

如果已在 `.env` 配置 `ALIOTH_DATABASE_URL`，可直接执行：

```bash
.venv/bin/python -m alembic -c services/api/migrations/alembic.ini upgrade head
```

当前 Alembic 环境使用 `asyncpg`，会自动把普通 `postgresql://` 地址转换为
`postgresql+asyncpg://`。如果新增 migration 涉及 PostgreSQL 专有能力，需在本地数据库
执行 `upgrade head` 验证通过后再提交。

## App PNG 真实数据库验收

`test_app_postgres_integration.py` 只接受显式注入的隔离数据库地址，未设置
`ALIOTH_TEST_DATABASE_URL` 时会显示为 skipped，绝不会回退连接
`ALIOTH_DATABASE_URL` 或 Alembic 配置中的默认地址。测试启动时会对该隔离库执行
`alembic upgrade head`，随后验证 App Bootstrap 双 Worker 租约、PNG 对象存储引用、
Capture 重放与视觉观察幂等性。

如果进程同时设置了 `ALIOTH_DATABASE_URL`，测试会拒绝与
`ALIOTH_TEST_DATABASE_URL` 同址的配置，避免误写开发或正式数据库。请始终使用专用
测试库；该保护无法代替数据库账号的最小权限和环境级网络隔离。

```powershell
$env:ALIOTH_TEST_DATABASE_URL = 'postgresql://USER:PASSWORD@HOST:5432/alioth_test'
.venv\Scripts\python.exe -m alembic -c services/api/migrations/alembic.ini upgrade head
.venv\Scripts\python.exe -m pytest services/api/tests/test_app_postgres_integration.py -q
```

测试只会删除本次随机生成的 `qc_task`，由数据库级联清理对应 Session、Bootstrap、
Capture 与 Observation；PNG 文件使用 pytest 的 `tmp_path` 本地对象存储目录，并在每个
案例结束前检查是否存在未被 `qc_evidence_artifact.storage_path` 引用的孤儿文件。
