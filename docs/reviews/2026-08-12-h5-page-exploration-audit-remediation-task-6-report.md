# Task 6：H5 Assessment 与 Evidence Inventory 持久化报告

日期：2026-08-13

## 交付结论

- Alembic `20260812_0049` 严格继承 `20260812_0048`，建立 `qc_h5_page_type_assessment` 与 `qc_h5_evidence_inventory_item`、四个归属 supporting unique index、复合外键、状态/JSON/计数约束及查询索引。Evidence Inventory 本任务只建 schema，行为由 Task 7 实现。
- Assessment 以 `page_capture_id` 作为跨 schema 的唯一自然身份。SQL `ON CONFLICT` 在同一原子语句内校验 task/run/session 归属、schema 单调、`expected_row_version` 和状态偏序，不依赖 Python 预查。
- 状态偏序明确为：`evidence_insufficient < not_found < {covered, requires_auth, blocked_by_safety, not_applicable}`；集合中的四个终态互不可覆盖，仅允许保持同一终态。相同 hash 可无版本条件幂等重放；不同 hash 必须携带当前 `row_version`，成功后版本加一；旧 schema 一律不能覆盖新 schema。
- payload hash 覆盖归属、完整规范化 detection 和规范化 assessment；递归剔除调用方提供的 `payload_hash`、`assessment_record_id` 与 `assessment_id`，拒绝 NaN、非字符串 JSON 键及非 JSON 结构。`assessment_id` 一律由服务器按 task/run/page type/Catalog 重建并覆盖，调用方在 wrapper 或 inner 中提供、缺省或伪造均不改变 hash。
- 完整 detection 会作为 `assessment_payload.detection_result` 留存；confidence、risk flags、evidence IDs、Catalog 和模型 provenance 等任一事实变化都会改变 hash，递归键顺序不影响 hash。task/run/capture UUID、检测 Capture 和单 Capture `page_capture_ids` 均 fail closed。
- Workflow 构造必须显式提供 Assessment persistence port，不再存在测试兼容 no-op。生产组合根注入真实 PostgreSQL 仓储，测试逐个显式注入 recording/failing fake。
- accepted、blocked、skipped 三种 Adapter 结果都在首轮发布或后继 prepare 前持久化；blocked 映射为 `evidence_insufficient` 并保留错误原因，skipped 映射为 `not_applicable`。nonaccepted Adapter 嵌套 owner/Capture 身份递归校验，错归属 fail closed，合法 caller `assessment_id` 也会被服务器派生值覆盖。任何持久化失败都会阻断 Work 发布。
- downgrade 先检查两张表；任一表存在数据即 `RAISE EXCEPTION`。空表时按依赖逆序删除查询索引、新表和四个 supporting index。
- 变更仅作用于 H5 Assessment/Workflow 及新 H5 表，不修改 App 工作流、菜单或路由，并保留 Task 4/5 既有链路。

## RED / GREEN 证据

1. 显式端口 RED：缺少 `assessment_repository` 的 Workflow 构造未失败；GREEN 后参数必填且 `None` fail fast，所有测试显式注入端口。
2. 非 accepted 持久化 RED：blocked/skipped 事件序列只有 `publish`；GREEN 后均为 `assessment -> publish`，且 failing fake 只产生 `assessment`、没有发布。
3. 跨 schema RED：v1 covered→v2 insufficient、v2 covered→v1 insufficient、旧 schema 强状态覆盖新 schema等用例均可写入；GREEN 后数据库冲突身份改为单 Capture，schema、状态偏序和 row version 在 SQL 内原子判断。
4. 状态偏序 RED：线性 rank 会允许 `not_applicable`→`evidence_insufficient` 以及 `requires_auth`→`blocked_by_safety`；GREEN 后终态互不可替换，`not_found` 仍可提升为明确终态。
5. 第一轮规范化 RED：wrapper/inner caller hash、错 Capture、错 task/run、NaN 和非字符串 JSON 键未全部 fail closed；GREEN 后身份先校验、调用方 hash/record identity 递归剔除。
6. 第一轮完整事实哈希 RED：只修改 detection `page_state` 不改变 hash；GREEN 后归属和持久化 detection 列纳入规范哈希，变更必须经过 row version 条件。
7. Round 2 assessment identity RED：wrapper `assessment_id` 造成 hash 漂移，inner 伪造 ID 被拒绝而非统一去信任，共 `2 failed`；GREEN 后 wrapper/inner 的缺省、合法或伪造值均由服务器覆盖，定向 `2 passed`，持久化内容只含服务器派生 ID。
8. Round 2 完整 detection RED：confidence、risk flags、evidence IDs、provenance 变化未改变 hash，Catalog 变化虽改 hash但未留存 detection，NaN/set 被裁剪后绕过，共 `7 failed`；GREEN 后完整 JSON 规范化、留存并入 hash，含键序稳定用例共 `8 passed`。
9. Round 2 nonaccepted 嵌套身份 RED：blocked/skipped 的 task/run/Capture 错归属未拒绝，合法 inner 未注入服务器 ID，共 `10 failed`；GREEN 后递归身份 fail closed、caller ID 净化，与无 inner 正常映射一起 `12 passed`。
10. 真实 SQL 测试入口新增：使用 asyncpg 及隔离库 guard，覆盖跨 schema 并发竞争、幂等投影和 row version；当前环境缺少隔离数据库变量，因此按门禁跳过而未连接开发库。

## 已运行验证

```text
pytest services/api/tests/test_h5_exploration_audit_remediation_migration.py \
       services/api/tests/test_h5_page_type_assessment_adapter.py \
       services/api/tests/test_h5_exploration_workflow.py \
       services/api/tests/test_h5_page_type_assessment_repository.py \
       services/api/tests/test_h5_page_type_assessment_postgres_integration.py -q
# 69 passed, 1 skipped

ruff check <Task 6 Python targets>
# All checks passed

pytest <Task 3–5 关键 + Task 6 + asyncpg 入口> -q
# 148 passed, 1 skipped

pytest <基础、App、Common Capture 与 H5 至 0049 迁移合同> -q
# 16 passed

python -m compileall -q <Task 6 Python targets>
# exit 0

python -m alembic -c services/api/migrations/alembic.ini heads
# 20260812_0049 (head)

git diff --check
# exit 0
```

上述 pytest 会报告 Agent Framework 的两个实验性 API 警告，不影响结果。

## 真实 PostgreSQL 风险与上线门禁

当前环境未设置隔离的 `ALIOTH_TEST_DATABASE_URL`，真实 asyncpg 用例已安全跳过，因此不能声称已验证实际 PostgreSQL DDL/锁竞争。上线前必须在隔离 PostgreSQL 中执行：0048→0049 upgrade、四个 supporting unique index 与全部复合外键创建、相同 Capture 跨 schema 并发写、空表 downgrade，以及任一新表非空时 downgrade 拒绝。不得在共享或生产数据库执行 destructive downgrade 验证。
