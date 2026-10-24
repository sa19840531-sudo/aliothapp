# infrastructure/storage 对象存储

封装截图、DOM 快照、可见文本、报告等大体积产物的存储访问。当前提供本地文件和 MinIO 两种实现。

## 职责

- 统一的对象存储读写接口，供 [domain/task](../../domain/task/README.md)（页面采集）、[domain/evidence](../../domain/evidence/README.md)（证据产物）、报告生成 Tool 使用。
- 落地设计文档约定的存储路径与命名规范（两级分类：`qc/{task_id}/pages/{page_seq}-{page_slug}/baseline/` 与 `.../rules/{rule_seq}-{rule_code}/`），详见[模块交互流程细化设计 3.1 节](../../../../../../docs/design/2026-07-01-alioth-qc-workflow-detail.md)。
- 产物保留周期与清理策略：见 domain/evidence/lifecycle.py 与 pplication/evidence_lifecycle.py；配置项 ALIOTH_EVIDENCE_RETENTION_DAYS / ALIOTH_EVIDENCE_GC_ENABLED（默认关闭物理删除）；合法留存表 qc_evidence_legal_hold，审计事件 qc_evidence_lifecycle_event。

## 当前实现

- `local_evidence_store.py` 默认写入 `.alioth/evidence`，可通过 `ALIOTH_EVIDENCE_STORAGE_ROOT` 覆盖。
- `minio_evidence_store.py` 将证据写入配置的私有 Bucket，数据库保存 Bucket 内对象键。
- 通过 `ALIOTH_EVIDENCE_STORAGE_BACKEND=local|minio` 选择存储后端；MinIO 使用 S3 API Endpoint。
- 文件路径使用 `{task_id}/{page_capture_id}/{artifact_id}.{extension}`，数据库保存相对路径、内容类型、SHA-256 与字节数。
- 单帧截图以 PNG 二进制保存；DOM、可见文本分别保存为 HTML、TXT；HAR、控制台日志和多帧截图序列保存为 JSON。
- `.alioth/` 已加入仓库忽略规则，本地证据不得进入 Git。
