# scripts/ 开发与运维脚本

## 本机部署

| 脚本 | 用途 |
| --- | --- |
| [start-local.ps1](start-local.ps1) | Windows 一键 Docker 部署（含迁移） |
| [start-docker-emulator-stack.sh](start-docker-emulator-stack.sh) | Linux：平台 + ReDroid + Mobile Host（Docker 模拟器整合） |
| [start-mobile-demo-docker-redroid.ps1](start-mobile-demo-docker-redroid.ps1) | Windows：Host Runner 连 Docker 映射的 ReDroid :5555 |
| [verify-docker-emulator.sh](verify-docker-emulator.sh) | 验证 ReDroid adb + Mobile Demo API |
| [setup-linux.sh](setup-linux.sh) | WSL/Ubuntu 内部署 |
| [sync-to-wsl.ps1](sync-to-wsl.ps1) | 将项目同步到 WSL `~/alioth` |

```powershell
# Windows 完整部署
.\scripts\start-local.ps1
```

```bash
# WSL Ubuntu 内（需先 sync-to-wsl 或 git clone）
cd ~/alioth && bash scripts/setup-linux.sh
```

## 种子数据

```bash
docker exec alioth-api python scripts/seed_first_ruleset.py
```

脚本默认读取 [../docs/data/alioth_first_ruleset_qc_ruleset_seed.csv](../docs/data/alioth_first_ruleset_qc_ruleset_seed.csv)
和 [../docs/data/alioth_first_ruleset_qc_rule_seed.csv](../docs/data/alioth_first_ruleset_qc_rule_seed.csv)，
按 `qc_ruleset.ruleset_code/version` 与 `qc_rule.ruleset_id/rule_code` 幂等写入。
`qc_ruleset.snapshot_hash` 由脚本根据规则集与规则 CSV 的规范化内容计算并写入。

## 基础产品数据

导入基础产品（项目编号、产品名称、H5链接、所属部门、负责人、平台）：

```bash
.venv/bin/python scripts/seed_products.py
```

脚本默认读取 [../docs/data/alioth_products_seed.csv](../docs/data/alioth_products_seed.csv)，
按 `qc_product.project_code` 幂等 upsert 写入。新环境初始化与每次部署时可重复执行：
先 `alembic upgrade head` 建表，再运行本脚本灌入产品基线（战略产品、核心产品两张表 + 早期演示产品）。
CSV 列为 `project_code,product_name,h5_url,department_name,owner_name,platforms`，
`platforms` 用 `|` 分隔，取值 h5 / web / app / ios。
`status` 与 `tenant_id` 不在覆盖范围内：管理平台上的人工启停结果会被保留，种子产品一律是运营中心维护的共享产品。
CSV 里出现的部门名会同步登记到体验中心的「部门管理」（已有的部门不动），部门账号才能选到这些部门。

## Agent能力注册表

将当前运行时 Capability Manifest 同步到 `qc_agent_capability_registry`：

```bash
.venv/bin/python scripts/sync_capability_registry.py
```

脚本读取根目录 `.env` 中的 `ALIOTH_DATABASE_URL`，按 `capability_name/version` 幂等更新
Skill与Tool能力投影。Manifest中已不存在的非归档能力会改为`disabled`，同步过程不会删除历史记录。

## 浏览器崩溃恢复验收

部署后在 Worker 容器内真实终止一条独立 Chromium 会话，并验证
`PAGE_CAPTURE_SESSION_CRASHED → goto 重建会话`：

```bash
docker compose \
  --env-file /opt/alioth/config/compose.env \
  -f infra/docker-compose.production.yml \
  exec -T alioth-worker \
  python scripts/verify_browser_crash_recovery.py \
  --url https://example.com
```

脚本只操作自己创建的独立验收会话，不会终止 Worker，也不会注入业务任务。
该脚本证明页面采集层能识别并重建崩溃会话，不等同于 W46-H5-103
完整任务验收。完整验收还必须从任务 `page-timeline` 的 `browser_recovery`
核对有界 attempt、原/恢复 Session 谱系、状态等价事实、崩溃前动作和恢复截图。

## H5 缺失证据验收

`W46-H5-104` 必须由隔离环境的 `evidence_storage_failure_driver` 触发，不能用普通
页面或人工修改任务状态替代。任务终态后，`run_h5_acceptance_matrix.py` 会同时核对
任务时间线与报告中的 `h5-evidence-preflight.v1`：缺失对象必须进入
`evidence_insufficient`，对应 Inventory 不得发布 Inspection Work，坏 Capture 不得产生
`pass/fail/warning/manual_review` 规则结果，Completion Gate 必须留下明确阻断原因。

## H5 模型合同失败验收

`W46-H5-105` 必须在隔离环境使用 `llm_contract_failure_driver` 注入一次不完整的
Explorer 结构化响应。存在唯一、低或中风险且已验签的 selected 候选时，运行时允许
执行平台确定性降级动作，并在 Action 参数中保存
`h5-explorer-decision-fallback.v1` 和稳定原因码；没有安全候选时，Work 必须以
`H5_EXPLORER_OUTPUT_INVALID` 进入有界重试，不能写成成功。

任务终态后，`run_h5_acceptance_matrix.py` 会从 `h5-model-contract-audit.v1` 校验
降级原因、Action 完成状态和结果 Capture 截图。普通页面、修改任务状态或只跑单元测试
不能证明该故障场景已经通过。

## H5 持续不稳定页面验收

`W46-H5-102` 使用页面行为夹具持续改变 DOM，并保持阻断加载状态。任务时间线只投影
Capture 中已有的稳定性安全摘要：结果、阻断/动态原因、采样次数、稳定样本数和 URL
哈希，不暴露页面正文或明文 URL。

批次验收要求任务以 `review` 收口、Run 为 `failed/incomplete`、报告与时间线均明确
未完成；至少一个阻断 Capture 必须保留采样次数、阻断原因和截图。该页面不得产生
`pass/fail/warning/manual_review` 正常规则结果。

所有携带 Timeline 的 H5 批次验收都会比较报告与时间线中的 `complete`、
`quality_status`、`requires_review` 和 `terminal_reason`。任一字段缺失或不一致都会以
`H5_TERMINAL_CONSISTENCY` 失败，不能由场景专项检查掩盖。

## 存量证据附件身份回填

在启用 `qc_evidence_artifact.checksum/size_bytes` 非空约束前，先执行默认 dry-run：

```bash
python scripts/backfill_evidence_artifact_identity.py \
  --report reports/evidence-artifact-identity-dry-run.json
```

确认报告中 `ready_for_constraint` 与冲突、缺失对象明细后，再执行实际回填：

```bash
python scripts/backfill_evidence_artifact_identity.py \
  --apply \
  --batch-size 500 \
  --report reports/evidence-artifact-identity-apply.json
```

退出码 `0` 表示扫描结果已满足约束，`2` 表示仍有待回填或冲突/缺失/异常数据，
`1` 表示配置、数据库或对象存储运行失败。对象读取采用有界重试；报告记录 execution ID、
起止时间、代码 commit、存储后端、批大小、扫描计数、task/capture/artifact 身份和原因码。
`storage_path` 只保存 SHA-256 摘要，不输出原始对象键，也不包含证据正文。

## App 页面探索部署验收前置检查

真实 App 页面探索验收前，先执行只读前置检查：

```bash
.venv/bin/python scripts/check_app_exploration_acceptance_prerequisites.py --json
```

脚本只检查隔离 PostgreSQL、Redis、MinIO、App 视觉 Provider 与 Official App 合同变量是否
具备，不连接任何外部系统，也不回显地址、Token 或密钥。输出 `ready_to_run` 仅表示可以开始
真实故障注入；PostgreSQL/Redis/对象存储/视觉 Provider/设备/Worker 故障和双 APK 多页面探索
仍必须实际执行，Fake 或 Mock 结果不能替代部署验收。

## H5/App 主证据规则投递验收

完成真实浏览器或 Official App 运行后，用系统运维账号只读核对
`Publication → Outbox → Inbox → Inspection Work` 持久化闭环：

```bash
read -rs ALIOTH_ACCEPTANCE_ACCESS_TOKEN
export ALIOTH_ACCEPTANCE_ACCESS_TOKEN
.venv/bin/python scripts/verify_delivery_acceptance.py \
  --base-url https://alioth.example.com \
  --task-id '<task_uuid>' \
  --agent-run-id '<run_uuid>' \
  --deployed-commit '<git_commit>' \
  --config-snapshot-id '<deployment_config_snapshot>' \
  --output reports/delivery-acceptance.json
```

Token 只通过 Authorization Header 发送，不进入 URL 或报告。报告固定记录
task、run、部署 commit、配置快照和每个 Publication 的 Outbox/Inbox/Work 状态；
退出码 `0` 表示持久化投递检查通过，`2` 表示仍有非终态或死信，`1` 表示接口/配置错误。
脚本不执行故障注入，且当前版本明确不检查补采；真实 PG/Redis/Worker、H5 浏览器和 App
设备验收仍需单独执行。
