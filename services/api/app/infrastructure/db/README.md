# infrastructure/db 数据库访问

PostgreSQL 访问层，包含 ORM 模型定义与 [domain/](../../domain/README.md) 仓储接口的具体实现。

## 职责

- ORM 模型（如 SQLAlchemy Model），与 `domain/` 的实体做映射转换，避免领域实体直接耦合 ORM 细节。
- 仓储实现（Repository Impl），实现各领域子域定义的仓储接口。
- 进程级 PostgreSQL 连接池（`pool.py`），按数据库 URL 复用并并发安全地初始化，默认 `min_size=1,max_size=5`。
- Agent能力注册表同步仓储（`capability_registry_repository.py`），在单事务和 advisory lock 下幂等写入当前 Capability Manifest，并停用已移除能力。
- pgvector 向量检索的具体查询实现（供知识库检索 Tool、情景/语义记忆检索使用）。

生产 Repository 与 Store 不得直接调用 `asyncpg.create_pool`，统一通过 `get_postgres_pool` 获取共享池；测试可继续通过构造函数 `pool=` 注入隔离实现。

## 表命名约定

沿用设计文档约定，业务表统一前缀 `qc_`（如 `qc_task`、`qc_rule`、`qc_finding`、`qc_page_capture`、`qc_agent_run` 等），具体清单见各 [domain/](../../domain/README.md) 子域 README。

## 关联

- 数据库迁移脚本存放于 [../../../migrations/](../../../migrations/README.md)（与本目录的 ORM 模型定义配合）。
