# Task 7 验收执行记录

状态：Task7三站执行与审计已收口，Not Ready审查的四项集中修复已完成验证，等待复审；三站业务验收未全通过（首站review、第二/三站failed）。不得将代码回归通过等同于真实站点通过。工作树 `web-catalog-navigation-depth`，起点 `c7b42e2e`，最终live实现固定 `8de64e67`；本轮审查修复尚未部署，以下旧轮按时间保留，不与最终轮混用。

## 验证与调试证据

- 聚焦 Web 套件：483 passed，51.09 秒，0 失败。
- 安全 CLI RED：缺少 username/password-env/adult-statement-env/output-dir 导致 2 项失败；新增 3 项错误输入用例。
- 安全 CLI GREEN 与原 runner：165 passed，9.51 秒。
- PG/迁移初跑：104 passed，5 个 E2E fixture 错误。根因是 E2E 仅允许旧隔离容器 `127.0.0.1:55439 / alioth_test_web_v2 / alioth_web_gap`；本次授权库为 `alioth-postgres:5432 / alioth_test_unbounded_menu / alioth`。增加精确四元组允许列表，保持 test/primary 数据库身份比较，不允许主库作为测试库。
- 第二次 PG：104 passed，5 个 asyncpg DSN 格式错误。SQLAlchemy DSN 的 `postgresql+asyncpg` 在正式 E2E 没有归一化；与相邻预算 fixture 一致改成 `postgresql` 直连格式。
- 第三次 PG：107 passed，2 failed（46.92 秒）。两项分别单独复现，按 systematic-debugging 技能追查，不修改生产门禁。
- 页型事实用例单独复现：1 failed，7.35 秒。断言前 Recorder 已返回 classified/content_list；回读 SQL 写死 `web-page-type-assessment.v2`，生产 Recorder 与仓储默认已升级 v3。假设为测试回读版本过期，只把该 SQL 更新为 v3 后重验。
- 页型事实测试最小修正后独立 1 passed（4.58 秒）。
- 收口用例单独复现 1 failed（11.50 秒）：完整 traceback 为 `task_finalization_repository._load_validated_web_coverage_snapshot → web_coverage_snapshot_row._stored_applicability → applicability snapshot_protocol_version 无效`。生产快照已 v3，但 Completion Gate 使用的独立完整行读取器只接受并强制重建 v2；先前协议测试覆盖的是另一个仓储同名函数。
- 最小假设验证：新增独立读取器 v2/v3 哈希与无效协议测试，RED 为 1 failed / 13 passed（4.64 秒）；生产只新增 v3 接受并保留原 protocol，领域层仍重验 normalization、分母和哈希。协议/快照/绑定关联 GREEN 106 passed（10.18 秒）。真实收口进入了原有报告绑定故障注入和恢复；剩余 fixture 的四类缺口预期更新为五类（含 home）与 v3。
- 最终完整 PG/迁移/E2E：109 passed，43.45 秒，0 失败；命令显式测试库为 `alioth_test_unbounded_menu`。
- 证据导出 RED：缺少导出入口 3 failed；加入同 API 精确票据路径、禁止重定向、摘要复核、票据不落盘后，CLI/导出 GREEN 8 passed（4.35 秒）。
- 主库升级：`docker exec alioth-api python -m alembic -c services/api/migrations/alembic.ini upgrade head` 输出 `20260912_0110 -> 20260912_0111`；`current` 输出 `20260912_0111 (head)`。

## 隔离与保密

所有破坏性 PG 测试只使用新建的 `alioth_test_unbounded_menu`。主库 DSN 仅供隔离身份比较，报告不记录 DSN、密码或令牌。聚焦和完整 PG 套件通过后才升级主库。

成年声明实际持久来源为 `D:/project/Python/alioth/infra/env/.env.local`；只读核验 enabled=true、version=adult-v1、declared_by/declared_at 均非空。API 进程的对应配置已生效，worktree 自身无 `.env`；不能以 worktree 文件不存在误判运行配置失效。使用现有配置，不新增或提交任何秘密。

## 已固定实现与二次重启

实现提交 `b5041376`（73 个文件）：移除新任务执行白名单，runtime 标识只建立首页身份而不限制后续菜单页递归；NULL 默认发现额度贯通领域/API/PG；修正命中语义、证据接口、实况报告 runtime 身份与实际账本校验。相关预存广告、清障、lineage、配置和 0109/0110 集成依赖一并纳入，未新增秘密。

最新提交前验证：完整聚焦+CLI/导出 500 passed（54.45 秒）；完整 PG/迁移/E2E/运行时授权 118 passed（38.56 秒）；策略/入口 132 passed（17.60 秒）；报告/协议 186 passed（15.03 秒）。新增真实 PG 25 层/25 次领取、唯一槽位 1..25、重建仓储后第 25 项只返回 already_reserved。Ruff 新文件检查通过，`git -c core.safecrlf=false diff --check` 及 cached 检查无错误。

执行 `docker restart alioth-api alioth-worker`，没有 build：

- API running，StartedAt `2026-09-12T11:43:10.561714595Z`。
- Worker running，StartedAt `2026-09-12T11:43:12.155620628Z`。
- 两者镜像均未变：`sha256:dc6f08fdb4de1675c250205490cc1c01874e3a5a6b70fe293eef08725ae043f7`。
- 重启后 Alembic `current` 为 `20260912_0111 (head)`；`GET /api/v1/health` 返回 status=ok、service=Alioth API、environment=local。API/worker 未配置 Docker healthcheck，不能称其 Docker health=healthy；PostgreSQL/Redis 原 healthcheck 为 healthy。
- 新登录实测开始于 `2026-09-12T11:43:51.388702+00:00`，fresh_login=true、token_stored=false，adult source=persistent_web_configuration。初跑完整输出已可恢复移动至 `superseded-run/`，首站补取证据在 `first-run-diagnostics/`；根目录只用于提交后重跑结果。

## 可复核命令

以下 `docker` 均指本机 DockerDesktop 的现有 CLI，不构建镜像。

```text
docker exec alioth-api sh -lc 'env -u ALIOTH_DATABASE_URL python -m pytest services/api/tests/test_web_page_type_catalog.py services/api/tests/test_web_page_classification.py services/api/tests/test_web_desktop_navigation.py services/api/tests/test_web_navigation_recognition.py services/api/tests/test_web_menu_exploration.py services/api/tests/test_web_runtime_discovery.py services/api/tests/test_web_list_detail_navigation.py services/api/tests/test_web_primary_content_visitor.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_six_site_shadow_fixtures.py services/api/tests/test_web_live_acceptance_cli.py services/api/tests/test_web_live_artifact_export.py -q --tb=short'

docker exec alioth-api python -m alembic -c services/api/migrations/alembic.ini upgrade head
docker restart alioth-api alioth-worker
docker inspect --format '{{.Name}} {{.State.Status}} {{.State.StartedAt}} {{.Image}}' alioth-api alioth-worker
docker exec alioth-api python -m alembic -c services/api/migrations/alembic.ini current

docker exec -e ALIOTH_LIVE_PASSWORD alioth-api python scripts/web_v2_six_site_shadow.py --live --api-base-url http://127.0.0.1:8000 --username admin --password-env ALIOTH_LIVE_PASSWORD --adult-statement-env ALIOTH_WEB_ADULT_STATEMENT --target-url https://heiliao.com/ --target-url https://hl365.com/ --target-url https://both.pnsmxatxp.cc/ --output-dir outputs/web-live-20260912-three-sites
```

PG 命令在容器 Python 中用 `urlsplit` 读取现有 DSN，仅替换 path 为 `/alioth_test_unbounded_menu` 设置 `ALIOTH_TEST_DATABASE_URL`，保留原 `ALIOTH_DATABASE_URL` 供隔离守卫比较，再执行 pytest：`test_web_exploration_budget_ledger_migration.py test_web_exploration_budget_ledger_repository.py test_web_exploration_budget_ledger_postgres_integration.py test_web_runtime_discovery_postgres.py test_web_v2_formal_report_e2e.py test_web_discovery_authorization_postgres.py -q --tb=short`（均位于 `services/api/tests/`）。不输出任一 DSN。

密码仅由用户提供值设置宿主进程环境，再 `docker exec -e ALIOTH_LIVE_PASSWORD` 继承；finally 删除该进程环境项。登录 JSON 不输出，令牌仅内存使用。证据票据必须是相同 API 的精确相对路径，禁止跳转，下载摘要核验后落盘且票据不落盘。

## 实况发现与补充裁定

- 扩展回归最终 1644 passed / 5 skipped / 0 failed（209.94 秒）。最初 36 个失败均在完整 lineage 模块：旧夹具缺少生产要求的正向 hit_test；历史十限额场景须显式冻结 menu_limit=10。只修夹具后完整模块及扩展套件通过。
- 已无 build 重启 API/worker；首站于 11:08:51 UTC 开始，正常收口 review/sealed，29 个静态目录行 allowlist_drifted、菜单/详情动作均 0，不能判通过。
- 证据接口 500 的完整错误链为 task_insight.web_evidence → PostgresWebCaptureEvidenceRepository.list_for_task:738 → int(NULL) TypeError。最小 NULL 回读与 HTTP500/403 分类 RED 3 failed / 36 passed；修复后证据包/仓储/CLI/导出 GREEN 110 passed（11.06 秒）。初次失败保留，不能写成站点网络失败。
- 首站同 Run 只读补取 16 captures / 80 artifacts，80 件全部下载成功。所有导航候选为 0，真实快照含可见同源并列分类与正向中心命中。根因：page_capture.py 的 pointer_interception=true 表示目标接收指针（pointerEvents 非 none 且 centerHitsTarget），导航门误认为被拦截。只在内存清除此字段后原证据恢复 7 个候选（证据 first-run-diagnostics/navigation-hypothesis.json）。最小正/负 RED 1 failed / 4 passed；删除错误否决且保留中心命中/视觉遮挡否决后，导航/lineage/capture-policy 119 passed（13.39 秒）。未硬编码站点 selector。
- 用户补充 Ruling：移除新任务 Web 执行白名单；历史字段仅兼容读取/审计，不删除历史数据库字段和迁移数据。同源、危险操作、hit-test、稳定并列容器、去重、lineage 与共享 deadline 保持。
- 影响定位：新策略发布仍生成完整 allowlist 与旧 discovery.v1 record_only / 10；运行时发现授权还受 max_unlisted_menus<=10 限制。补充实施范围包括发布策略的 runtime-only 首页上下文、默认启用不限数量发现、可空上限领域/API/SQL授权验证、旧快照只读兼容测试。旧代码已执行站点须在修复重启后重新实测，初跑材料单独保留。

## 第二轮实况诊断（实现固定 b5041376）

- 提交后扩展回归：1644 passed / 5 skipped / 0 failed（210.38 秒），三站期间不改变运行实现。
- heiliao.com：Task `7be4a180-8368-4d89-9ad6-cadb14cdecae`，Run `45000db1-5e59-45e3-a8b7-fd9834908ea9`。策略为 runtime-navigation.v1、discovery enabled=true、menu_limit=NULL、max_unlisted_menus=NULL，deadline 为 14:43:52 UTC。发现 33 个候选，首页 1、菜单已预留 1、成功菜单 0、详情 0；首个“最新黑料”动作在 Resolver 被 `WEB_DISCOVERY_NOT_AUTHORIZED` 拒绝，最终 `WEB_NAVIGATION_CURRENT_UNTRUSTED` 安全收口。此为内部缺陷，不是外站失败，不能视为验收通过。
- 根因追踪：`ActionIntent.__post_init__ → freeze_mapping → capture_runtime SemanticTargetResolutionRequest(parameters=intent.parameters) → PostgresWebDiscoveryActionAuthorization.authorize`。真实 ActionIntent `action-execution:600fa764af66cba1d99ce1e6125c8b7c1a5f41213103f8dee615259557de4e23` 的候选与领取 manifest 来源一致；比较器只转换 menu_path。只读反序列化实验得到 stored_vs_frozen_equal=false，差异为 exploration_path(list/tuple)、navigation_evidence(list/tuple)、navigation_container_restore_seed(dict/mappingproxy，嵌套数组不同)。仅用既有 thaw_mapping 递归还原后 recursive_thaw_equal=true。相邻 PG 授权测试直接传入普通 dict，漏测真实 ActionIntent 不可变边界。
- 最小假设：授权内容未漂移，拒绝来自深冻结数组表示差异。将先增加真实 ActionIntent→授权器的 PG 失败回归，再仅归一化候选 JSON；不得放松 SQL 的 task/Run/session/current Capture/动作序号/epoch/槽位门禁。
- 报告补充缺口：preview page_exploration 不携带完整菜单与广告账本，现有 CLI 只补了统计。需在鉴权后的同 Task/Run 证据接口补全持久化账本并导出其广告引用，不能将“未回读”伪装为 no_candidate。
- 最新 Ruling：历史 allowlist 策略只允许读取/审计；未完成旧 Run 恢复必须明确 fail-closed：`legacy_allowlist_policy_not_executable / requires_new_run`，不得静默重签或迁移身份。三站运行结束后单独测试并提交此兼容边界。

## 第二轮结束后的最小修复与第三轮准备

- 深冻结授权最小 RED：真实 ActionIntent→PG 授权比较失败；只使用既有 thaw_mapping 还原嵌套不可变 JSON 后，9 个 PG 授权（含嵌套篡改拒绝）及 29 个真实候选 Resolver 回归合计 38 passed（10.41 秒）。提交 `bded2a1e`；相邻 Resolver 旧夹具补正向 hit_test，没有改变授权条件。
- 历史策略恢复 RED：旧快照仍进入 Browser builder；新增运行入口门禁后 runtime composition/策略 123 passed（6.92 秒）。提交 `fa70de88`；历史快照仍可 materialize/read，未重签或迁移其身份。
- 第二站 hl365.com：Task `76c361c6-cae8-47b7-833a-49a6f03cb653`，Run `a4de3945-0b97-43fa-b87c-d2b64bf8993c`。首页 1、发现 18、菜单/详情动作 0；普通关闭动作 7 次均独立有界。广告 supplemental Capture `20c327f8-5d8e-4140-855e-b4ea3bab1948` 已保存，但结果 `return_failed/WEB_AD_LANDING_RETURN_FAILED`，不能算通过。
- 广告根因证据：该 Capture 的 action trace Artifact `f436b36f-4baa-40cd-a17d-4111b023dbc2` 中 DIRECT_AD_OUTBOUND 出现 12 次，obstruction_chain_index 为 1..12，new_page_count=closed_count=12。调用链为 unstaged SINGLE_TRIGGER→桌面策略自动 clear_obstructions=true→剩余清障链复用原 DIRECT_AD_OUTBOUND。最小动作轨迹回归 RED 为 12 != 1；仅对广告直开禁用清障链，普通弹窗关闭的有界重试保留。
- 持久一次性 claim Ruling：复用 qc_surface_phase_run 的唯一键和 running/completed/failed 状态，浏览器打开之前原子领取；同 Run+logical_page_id 跨 Worker/重启最多一次，失败/未知结果仍消费。不增加迁移，不以自由文本 diagnostic 或进程锁代替持久权威。
- 持久 claim RED：3 项缺少仓储；GREEN：双连接并发只有一项领取、三态 Worker 重建不重领 3 passed（4.73 秒）。生产 adapter RED：2 项不支持持久 claim 参数；补接入口并验证 claim-before-browser、已消费零浏览器调用、失败后重建不重试。最终 PG 还覆盖错误 task/session/lease owner/token/sequence/epoch 拒绝。
- 报告根因：runtime 领域 to_dict() 使用扁平执行字段，但 report 校验读取了不存在的 execution 子对象；preview 只补数量，没有补持久菜单和广告账本；公开证据预算校验还写死 5/10，NULL 被当成零。最小 RED 覆盖 12 项真实扁平账本、NULL/12 上限、非法槽位与广告引用遗漏，再修投影，不改业务账本事实。导出额外包含 artifact_ids，成对截图复合 JSON 使用 .json 扩展。
- 自审 deferred minor：非持久 _ledger 遗漏 result.ad_landing_evidence 已先 RED 1 failed（4.98 秒），补字段传递。reserve_detail 的 WebRuntimeMenuEntry 注解已在前提交补齐；前置 lineage/ad 依赖已纳入 b5041376。
- 最新联合 GREEN：广告/普通清障/采集/公开证据/报告/导出 678 passed（155.73 秒）；菜单/广告/旧 Run/安全 CLI 154 passed（23.28 秒）；完整 PG/迁移/E2E/授权/持久 claim 122 passed（43.74 秒）。警告仅既有 Agent Framework experimental 和 pytest cache 权限，不作为失败隐瞒。
- 第三站 both.pnsmxatxp.cc：Task `7534b7c4-f7cd-44dd-afea-0f068aa521a0`，Run `f7d490a0-7fdb-4eeb-bb47-fb9d06059d84`。首页/菜单/详情正式页均 0；第二个动作被 COMMAND_HOST_BOUNDARY_VIOLATION 阻断，属于主机边界审计结果，不应归类网络错误，也不能为完成验收扩大到其他域。
- 第三轮重跑范围为三站：heiliao 因授权和共享广告链修复，hl365 因已确认重复 outbound，both 用新运行记录准确的安全边界结果。原第二轮输出及当前 175 件已导出 Artifact 将可恢复保留；新实现提交并无 build 重启后才开始新 Run。
- 提交前只读下游检查：使用第二轮真实 ActionIntent 和 source/before UI Snapshot 离线调用 resolve_web_discovery_target，输出 resolved、destination_url=https://heiliao.com/hlcg/；没有发起浏览器动作，也没有改变已结束的旧 Run。

## 第三轮（固定实现 2007a37f，发现后续缺陷，不作为最终通过）

- 原子提交 `2007a37f`，没有新增迁移；第二轮完整报告与原附件可恢复移动到 `superseded-runtime-run/`。首轮仍在 `superseded-run/`，没有删除任何旧验收材料。
- 无 build 重启：API StartedAt `2026-09-12T12:43:09.241147696Z`、Worker `2026-09-12T12:43:12.562626301Z`，均 running，镜像仍为 `sha256:dc6f08fdb4de1675c250205490cc1c01874e3a5a6b70fe293eef08725ae043f7`。Alembic current=`20260912_0111 (head)`，health 返回 `{"status":"ok","service":"Alioth API","environment":"local"}`。
- 新登录开始 `2026-09-12T12:43:45.986080+00:00`；审计记录 fresh_login=true、token_stored=false，adult-v1 来源 persistent_web_configuration、scope=account_session_site_independent。三站执行期间不再修改生产代码。
- 第三轮首站 Task `058363d8-d215-42e4-867f-3b7258db80d6`，Run `684c39ac-25e2-40db-8901-20ace8e062e6`，最终 review/preview。
- 旧第二轮只读补取位于 `second-run-diagnostics/`：首两站公开证据及 184 件 Artifact 全部保存，宿主 Get-FileHash SHA256 校验 184/184 一致，总计 53,187,921 bytes。第三站 evidence API 的 404 单独保存为 `site-3-unavailable.json`（旧 Run 未形成唯一 Web V2 预算与会话证据包），没有伪造空白成功。
- 额外扩大到所有 136 个 `test_web*.py`：2123 passed / 25 failed / 305 skipped（378.35 秒）。5 个失败模块独立重现为 25 failed / 17 passed（18.83 秒）：24 项旧候选/unknown-origin/URL identity 夹具未提供正向 runtime hit_test，另 1 项 browser guard 固定 Windows Chrome 路径。离线最小对比普通/SPA/unknown-hash 三组旧 HTML 均 0 候选，复用既有运行时夹具均恢复 2 候选；并未放松生产门禁。浏览器项仅使用测试 env `ALIOTH_TEST_CHROME_EXECUTABLE=/root/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome` 后独立 1 passed（3.48 秒）。最终 live 期间不修改生产或测试文件，待本轮结束统一补旧夹具后再验。
- 详情计数必须区分：持久 ledger 的 detail_action_count 是一次性预留消费；没有 detail_action_execution_id 不能宣称已点击。当前前两个绑定失败的 source Capture 分别为 `e0d60eba-fbef-499d-9a7c-9179285c446b` / `b99a61e2-6847-4b19-8678-3b39bda3a411`，52/54 个候选中各唯一匹配，URL 与 detail_destination_sha256 完全一致，选择器唯一；目标初始位于 y=1877.5 / 1687.11，离屏，随后滚动 Action 序号 16/20 均 completed。须继续核对滚动后就绪事实，不能据此直接称站点问题或平台 URL 归一化问题。
- 13:04 UTC 首站已形成一项真实 detail_action_execution_id，落地后结算 detail_page_type_mismatch；2 项只预留、点击前绑定失败。菜单预留 5、真实详情点击 1，不等于详情质检成功。13:05 UTC main Run 为 done，任务进入 exploration_draining；等待最终 Gate 与报告，不提前宣称通过。
- 首站最终为 review/preview，首页 1、真实菜单执行 5、详情预留 3/真实点击 1/合格详情 0；142 条账本中的其余 136 条为 129 个 WEB_DISCOVERY_UNAUTHORIZED_ACTION、4 个 IDENTITY_UNTRUSTED、3 个 CANDIDATE_STALE。menu_limit 仍 NULL。不能将这些批量拒绝自动认定为正常：本轮三站全部终态后按来源/可见性/动作/epoch/manifest 抽样只读核验，若平台缺陷则最小 TDD 修复并重跑。
- 第二站最终轮 Task `e85cbe20-0aab-49f9-8ca4-421560c1f891`，Run `fadde07f-a839-4825-98aa-e71c95f043e5`。13:20 UTC 广告真实成功：唯一持久阶段 completed / WEB_AD_LANDING_OBSERVED；首页账本 observed；Capture `69913667-123e-4a88-b614-5c470e37b134`、Action trace Artifact `2d385fdb-b169-4e42-afd8-8cdb1c983a62` 中 DIRECT_AD_OUTBOUND=1、new_page_count=1、closed_after_observation_count=1。旧轮 12 次重复没有复现。
- 报告术语待改：budget_conflict 当前校验同时覆盖额度与非成功账本终态，应拆开真正额度冲突和账本终态/形状问题；不能通过改术语降低 needs_review 或隐藏失败。

## 第三轮只读定位、裁定与逐项 RED/GREEN

- 三站结束时间 `2026-09-12T13:23:08.944046+00:00`；第三站 Task `bea3b3d6-cf42-4658-9142-b94e8dc02146`、Run `680e8232-5435-4f36-8363-984f165d3f2c` 为 failed/sealed。首导航后 host=adopt.rdidoricl.cc，动作 2 blocked/COMMAND_HOST_BOUNDARY_VIOLATION；没有合格业务页、菜单或详情，广告未尝试；未形成唯一 budget/session 公开证据包，捕获与动作审计仍保存。不得将跳转主机当作原站继续探索。
- 抽样来源、epoch、manifest、policy、reserve 与详情前后 Capture 汇总在 `runtime-outcome-audit.json`，由同目录 `audit_runtime_outcomes.py` 只读生成；不驱动浏览器、不修改 Run。第三轮引用附件 410 件均已下载，下载端执行 SHA256 校验。
- hl365 的 18 个候选全部在 Capture `22469607-2aa8-4b1d-98ab-b8e7d8d65124` / epoch 31，risk=obstructed、runtime_record_only、授权动作集合空、execution_slot=NULL，尚未 reserve。可见导航中心真实命中为 true，但正文卡片 span 的非模态 `web_obstruction_layer_kind=geometric_overlay` / structural=true 被 navigation_is_obstructed 扩成全页阻断。假设不是 manifest 条数上限，而是错误的全局遮挡事实；真实字段最小 RED 1 failed/4 passed，限定非模态几何层后导航识别 GREEN 36 passed（2.60 秒）；模态/验证码/年龄门和局部被覆盖仍拒绝。
- heiliao 129 个 unauthorized 中有 123 个 anchor obstructed（21 depth 1、102 depth 2），4 个危险 anchor、2 个 obstructed button。Capture `73e43a16-8dbb-4bc6-95b5-15120825563a` / epoch 27 的 ybrg 入口 x=3372.22，明显超出 1920 视口，属于真实安全拒绝，不是第十项限额；但将其作为可见待授权候选仍违反分母口径。按 x/y/中心边界且 include_blocked 两模式最小 RED 12 failed，排除离屏后两导航模块 GREEN 67 passed（5.17 秒）；不横滚/不展开隐藏区，原始快照仍审计。
- 详情两项 href/规范 URL 与 DB detail_destination_sha256 均一一匹配且 selector 唯一，因此未发现 URL 序列化/归一化缺陷。滚后 Capture `810484da-bd45-4a0b-b3c7-0ab1f0eeaafa` 中目标 y=41.5，中心命中固定栏；`c0f09943-46aa-43d1-952f-0b8fa1925797` 中 y=-148.89。负坐标被 _nonnegative_number 钳制为 0 后错误标为 visible：RED 3 failed/2 passed，保留有符号坐标后详情模块 GREEN 77 passed（10.30 秒）。
- 详情滚动的独立根因：resolver 已有 web_detail_candidate selector，但 _build_browser_action_request 仅接受正文 selector，详情变为通用页面 SCROLL。受控真实浏览器复制目标 y=1877.5 与固定栏，RED 精确复现滚后 y=41.5，另 selector=None 断言失败；仅透传来源模式相符 selector 后命中恢复。进一步观察真实轨迹仍 2 次 scroll，原因是 page-probe 进入通用 coverage 循环；限定 Web page-probe 有绑定 selector 为单次滚动、稳定采样和新 Capture，普通/H5 coverage 保留。联合 GREEN 85 passed（21.78 秒），包括真实 Chrome 正向命中与单动作，来源不符负例不透传 selector。
- 报告原因拆分 RED 2 failed；数值预算与账本闭环各自判定后证据包/运行组合 GREEN 154 passed（6.46 秒）。页型版本、子菜单残留等 23 个历史断言同步改为 ledger_closure_invalid，并明确不含 budget_conflict；needs_review 不变。
- 原 all-Web 25 个失败涉及的 5 个完整模块已在四份旧夹具补正向 hit_test、MAX(slot) 查询 mock 后 GREEN 42 passed（17.20 秒）。浏览器使用真实已安装 Chrome env，没有更改生产或跳过来掩盖失败。
- 后续 Ruling：修复提交且无 build 重启后至少重跑 heiliao 与 hl365。第三站保持上述明确跨主机失败；本轮改动只涉及候选几何/局部遮挡、详情 SCROLL 与报告原因，不改初始主机边界，不能以新策略恢复旧第三站或访问重定向主机。
- 本轮提交前完整验证：显式受控 Chrome、清除两个 DB 环境键，所有 136 个 test_web*.py 为 **2178 passed / 303 skipped / 0 failed**（368.58 秒）。与前次 305 skipped 差异为本次显式启用真实 Chrome 的两项受控浏览器回归。隔离 PG 七模块 **122 passed**（39.57 秒）；test_page_capture_tool.py 的 scroll/obstruction/ad_landing/outbound 定向 **17 passed / 352 deselected**（6.62 秒）。测试警告为既有 Agent Framework experimental、Starlette/anyio 弃用提示与 pytest cache 权限，无隐藏测试失败。
- 静态自审：新导航识别/候选适配/Driver selector 与新增导航、详情、浏览器测试六文件 Ruff 全通过；全十个触及文件的 Ruff 仍有旧大文件历史 E501（证据包、详情导航、page_capture 与证据测试），本次新增 E501/导入问题已修正，不宣称全仓库 lint 通过。git diff --check 无空白错误；无新增数据库迁移、无前端菜单/路由变更。无关 nav_alias、page/__init__ 与 test_page_nav_alias 的既有改动保持未提交。

## 第四轮（固定 1fe66b64）终态与只读失败边界

- API/worker 无 build 重启时间分别为 `2026-09-12T13:58:57.827516639Z` / `13:58:59.540660747Z`，image 均未变化；`/api/v1/health` 返回 ok。主库仍为 `20260912_0111 (head)`。使用内存 fresh login 和持久 adult-v1 配置，于 `14:00:44.727852+00:00` 至 `14:13:16.025898+00:00` 重跑受共享修复影响的 heiliao、hl365，输出 `viewport-fixed-rerun/`，引用附件 165/165 下载成功；该轮不合格，不能与前轮混称通过。
- heiliao Task `c8d56930-20f8-4e60-9d66-4c914fcea4d4`，Run `d635c304-5ce2-4c06-b535-d0ef63aff53d`：failed/preview，`WEB_NAVIGATION_CURRENT_UNTRUSTED`；冻结 deadline 为 `17:00:44.955515+00:00`，实际失败 `14:08:44.056732+00:00`，不是总时限耗尽。18 条 ledger 中首页 page_type_unclassified 1、未复核发现 16、navigation_failed 1；菜单真实执行 1、详情预留 1/真实点击 1/合格 0；两个业务逻辑页均 no_candidate/WEB_AD_CANDIDATE_NOT_FOUND，outbound=0。
- 详情获取与打开确实成功：`https://heiliao.com/archives/115143/` 唯一候选及 destination hash 一致；SCROLL 11 将目标滚至新 Capture `99943713-4ad5-4b7f-82b9-fb8d8de0da80`，scroll_offset=1237、visible=true、center hit=true、readiness accepted；CLICK 12 completed/execution_succeeded，落地 Capture `02871978-7ba2-4a50-a460-3f71b51c5514` URL 精确匹配、readiness=stable。随后 prepare → assessment agent → VerifiedWebAssessmentEvidenceLoader.load 在送模前失败。原容器日志未找到该栈；读取同一真实 DOM/PNG（归属/摘要/尺寸校验均经过）离线重放稳定得到 `RuntimeError: WEB_ASSESSMENT_SENSITIVE_CONTENT_PRESENT`，位置 `web_assessment_evidence.py:180`。相邻菜单 Capture `5b5ab6ae-1e4c-4610-a74a-1425c9b7c89b` 同载入器接受，排除通用 PNG/同 Capture 归属错误。
- 秘密命中只记录类别，不记录值：实际仅一个带 data-image-preview/data-image-zoom/z-image-loader-url 的 img.alt 纯 32hex；正文投影及 authorization/token/password/Bearer/JWT 均无命中。Ruling 明确成人声明不能放开凭据保护，改为限定资源标识先脱敏再检测。最小 loader RED 1 failed/8 passed；GREEN 完整模块 47 passed。真实 Capture 重放 GREEN：redaction_categories=[image_alt_resource_identifier]、projected_secret_detection=false，原值不送模；审计 `viewport-fixed-rerun/failure-boundary-audit.json`。
- 首页 Capture `172a9adb-94c4-4564-9dd9-b0c35b8809fa` 及主体补图 `33005693-660e-4d30-b726-2d11482a2fea`，模型 confidence 精确 0.70，原始/观察/候选均 home；模型因根页同时有重复视频卡片写 needs_review 导致最终未分类。机器 home 项为 top_level_obstruction_count=0、is_canonical_root_url=true、top_navigation_container_count=1、visible_category_entry_count=7、repeated_primary_card_count=46（媒体卡片 15，无单主体播放器），必需项通过、强冲突空。规则是布尔/计数门，无各项加权分数，不能臆造评分。Ruling：达到 >=0.70 且机器门通过无强冲突时软复核仅留诊断。RED 2 failed/3 passed；正例 0.70/0.71 与低分/阻断/真正缺失全部 OR 支持项的负例均已验证；初版负夹具仍保留重复卡片导致机器门正确通过，后按实际 OR 合同补正，并非修改生产门迎合夹具。
- hl365 Task `572f4a67-96e7-424e-ae23-4efb81bc8b1b`，Run `6dfeb217-9e77-4e2f-8352-9bcd18f1f138`：failed/preview，`WEB_HOME_TIMEOUT`；冻结 deadline `17:10:01.833953+00:00`，14:13:14 终止。只有 home_pending，菜单/详情/广告为 0。最后动作 3 为 OBSERVE 基线、completed，Capture `39b2dfed-c74a-4b51-a81d-03e274fb8d41`：stable_wait=15013ms、timed_out=true、quorum=false、DOM quiet≈2248ms、critical document/script pending=0、same epoch/capture consistency=true、human verification=false。settle outcome=inspectable_dynamic，DOM 176428 字符/截图存在/UI 141 元素；8 个视口媒体仅4个 ready，6个 localized loader、0 blocking loader。不是 3h 总预算或 900s prepare 无限循环，而是 15s 本地稳定窗口未满足。
- 旧 hl365 Snapshot 对部分可操作图片保存 complete/naturalWidth：浮层小图 complete=true/naturalWidth=0，确实终止失败；但正文大图仅 media 几何记录，没有解码字段，不能把其 pending=0 解释为失败。Ruling 纳入所有当前视口媒体的解码及资源终态持久事实：受控 Chrome RED 5 failed → GREEN5，已解码/关键失败/非关键失败/pending/离屏负例独立覆盖；指标降级合同 RED4 failed/1 passed → GREEN。degraded 不允许进入正式证据，关键未成像仍 timeout。自审另捕获旧90%比例掩盖关键失败的 RED1，修复后须组合与完整回归。
- 第三站保留上一轮跨主机拒绝，未重新打开跳转主机；已只读补全 `third-site-boundary-evidence/`，3 captures/15 artifacts 下载成功，`boundary-audit.json` 明确 action2 COMMAND_HOST_BOUNDARY_VIOLATION。此处结果不能写成本站可继续探索。
- 本次自审补充：重复 alt 可能令后置资源标识覆盖前置凭据，最小 RED1 failed/8 passed 后拒绝任何重复属性的资源标识特殊处理；完整 loader+全部启用的 Web Capture metrics GREEN76 passed（26.17 秒），六个新核心/测试文件 Ruff 全通过。包含最新重复属性用例但已导入旧函数的全137模块进程得到2203 passed/303 skipped/1 failed（382.28秒），唯一失败为该已修边界；不能把它写成全绿，已重新在固定实现及两个显式 Chrome env 下启动完整137模块。
- 其它新鲜验证：readiness/分类/loader/真实Chrome组合199 passed/5 skipped（34.49秒）；五项跳过由另一个 ALIOTH_WEB_TEST_BROWSER 开关控制，随后76项专项已全部实际执行。共享 page_capture 的 readiness/settle/obstruction/ad_landing/outbound/scroll 回归33 passed/336 deselected（7.70秒）；隔离 PG 七模块122 passed（39.89秒）。第四轮165/165附件宿主 SHA256 逐件核验，37,130,422 bytes、0 mismatch。
- 固定实现后最终全量命令：容器内清除 ALIOTH_DATABASE_URL/ALIOTH_TEST_DATABASE_URL，显式设置 ALIOTH_TEST_CHROME_EXECUTABLE 与 ALIOTH_WEB_TEST_BROWSER 为已安装 Chrome，`python -u -m pytest <全部137个test_web*.py> -q --tb=short -p no:cacheprovider`：**2209 passed / 298 skipped / 0 failed（400.13秒）**。全部测试运行期间无生产/测试代码修改；剩余跳过为未启用的PG/特定集成前置，PG七模块另测122 passed。准备固定原子提交后无 build 重启，两站用新 Run 重跑；第三站保留上一轮严格主机边界失败。

## 第五轮执行记录（固定 1b92d67b）

- 原子实现提交 `1b92d67b`，11 文件，仅本次应用/捕获/测试/规则/报告，未夹带无关三文件与原始站点附件。无新增迁移或前端菜单/路由同步。
- 第一次重启申请被自动审查以缺乏服务级授权拒绝，未执行；读取批准的 Task7 brief Step4 并获得主代理重申用户“重启docker无需build”授权后，使用同一个直接 Docker restart 命令重试成功，没有间接绕过。重启前 queued/running/paused Task=0；旧 agent_run 有25条 running 行挂在 failed/review Task（18/7），只记录历史残留，不恢复/更改旧 Run。
- `docker inspect`：API `2026-09-12T15:09:52.258713778Z`、worker `15:09:55.59455901Z`，均 running；image 仍 `sha256:dc6f08fdb4de1675c250205490cc1c01874e3a5a6b70fe293eef08725ae043f7`。Alembic current=`20260912_0111 (head)`，health status=ok；worker settings adult enabled=true/statement_version=adult-v1。
- 新登录与两站顺序实测从 `2026-09-12T15:11:02.480454+00:00` 开始，输出 `assessment-media-fixed-rerun/`；execution-audit 明确 fresh_login=true、token_stored=false、adult来源 persistent_web_configuration、scope account_session_site_independent。首站新 Task `0ac2b9aa-37cf-4ecb-98b6-38b89ca77b9b` / Run `4ce8c80e-9447-4a4b-ad74-2962c60c3630`，menu_limit=NULL、冻结 deadline=`18:11:02.881733+00:00`。本轮实测期间不改生产或测试代码。
- 首站在 `15:18:25.831585+00:00` failed/sealed，鉴权报告错误码 `WEB_HOME_TIMEOUT`（Task.last_error 未含标准码，不能混为无错误）。只有 home_pending 1；菜单/详情预留与执行均0，广告阶段未尝试，不能写成已检查且无广告。动作1导航、2–6普通清障、7基线观察均完成，没有 DIRECT_AD_OUTBOUND。
- 只读检查最后 Capture `3106eae3-4a57-4145-a53d-251a421e55b3`：22 loaded + 1 unknown；唯一 unknown 为 img，rect=(680,896,96,96)、viewport_coverage=0.0044444444、critical=false、complete=true/naturalWidth=0/naturalHeight=0、无source。core=true、loader=false、transition=false、DOM quiet=1414.6ms、required pending=0、同epoch/一致性=true，但 necessary_media_ready=false、settle=false、timed_out=true。调用链唯一假设：所有视口媒体状态集合将非关键空装饰图 unknown 传播为 viewport_media_not_ready，导致必要媒体门过度阻断；不是关键内容未成像或全局预算耗尽。
- 新 Ruling：阻断集合限于必要/关键媒体；上述非关键空装饰图保留 noncritical_media_unknown 警告但排除必要分母。关键主体或被页面语义明确标为必要的媒体 unknown/failed 继续有界超时；采样缺失/截断不能证明必要集合完整。待本轮第二站终态后，用真实 Capture 字段进行最小 RED/GREEN，不在 live 运行期间修改生产代码或测试。
- 口径进一步确认：采用 viewport-media.v2；critical=true 或 aria-required=true 为必要媒体，非关键 unknown/failed/pending 均分别保留警告和计数，不单独阻断 stable。历史 v1 不回填、不重新判定。非关键异常比例/面积若需质量复核，应作为独立 needs_review 而不能伪装为必要媒体未就绪；本次未凭空新增该质量阈值。
- 第二站新 Task `a491b4af-dd4d-45bb-87d1-96d9552fa0ac` / Run `a60af5bb-34c8-4ea1-a4b0-5941dc2f0c1c`，冻结 deadline=`18:18:28.397154+00:00`、menu_limit=NULL。首页准备在既有12层/900秒预算内收敛；继续菜单探索，不能提前称为终态。已完成的前三个广告持久阶段均 completed/WEB_AD_LANDING_OBSERVED；其 action_trace `39097cb5-6be7-4973-8ff2-df552612d3ac`、`1209cfbd-b3e6-437a-b3de-d906294b51bb`、`f2d1be41-e06e-470b-9639-5f94cdb86376` 各 DIRECT_AD_OUTBOUND=1、new_page_count=1、closed_after_observation_count=1、source state_diff.change_count=0。这是只读附件核验，不是重复打开广告。
- 第五轮遭遇基础设施中断：Docker Desktop LinuxEngine 的 container/exec API 返回500，原 live CLI 连接丢失；只读 inspect 确认所有 Alioth 容器在 `15:50:40Z` 同时重启，api StartedAt=`15:50:40.507325001Z`、worker=`15:50:40.508218611Z`，OOM=false、exit=0，触发方未知，本代理该时段未调用 restart。恢复后容器内 health=ok，但旧 Task 仍 running，最后动作26 completed，菜单槽1/2/3对应动作12/17/21已完成、槽4预留；详情0、广告4 observed。本轮必须标记 infrastructure_interrupted，不能记成目标站网络故障或正常终态。
- 中断前证据保全：`archive_interrupted_run.py` 只读导出351项，347项HTTP下载成功，宿主SHA256 347/347一致、98,152,776 bytes。其余4项均成对截图：数据库摘要与原存储JSON一致，但公开 evidence_file 转换为单PNG（不是同一字节表示），因此严格摘要检查正确拒绝。`artifact-download-diagnostic.json` 记录两侧摘要/尺寸；原件只读保存在 `storage-preserved/`，额外4/4宿主摘要与数据库相符，共351原件完整保全。没有关闭摘要校验、没有假称HTTP原件成功。
- 主代理裁定通过公开cancel审计收口中断任务，再做media-v2修复与新Run重跑两站。具体 POST `/api/v1/workstation/tasks/a491b4af-dd4d-45bb-87d1-96d9552fa0ac/cancel` 的工具执行被自动审批以“取消仍运行任务需要用户明确授权”拒绝，命令未运行；没有更换通道、直改DB或间接执行来规避。已向主代理报告审批边界，待用户授权或既有机制自然终态。取消接口无reason字段，infrastructure_interrupted只作为独立验收审计原因，不能伪称数据库已有该错误码。
- 后续新鲜只读结果：hl365由既有机制在 `15:52:24.594975+00:00` 自然 failed/sealed，活动Task=0；取消命令从未执行，删除未使用的本地取消助手，避免误操作。第五轮因基础设施中断不算最终两站验收，仍需新Run重跑；旧动作/广告未重放。

## 必要媒体 v2 的最小修复

- 真实未知节点字段（Capture 3106eae3）和受控浏览器复现 RED：14 failed/6 passed/28 deselected（19.97秒）；分别证明非关键三态被过度阻断、语义必要性漏计、96×96空图阻止真实稳定窗口，以及旧总体比例仍影响正式reader。补充必要数值/截断负例后最小 GREEN 24 passed/28 deselected（16.32秒）。
- 仅修改新采样的必要集合汇总与Web专属消费入口：v2所有items保留，关键/aria-required集合决定状态，非关键三态警告与计数单列；H5原比例字段不改，历史v1 reader不改。关键失败不能被比例或独立state=ready字符串掩盖，计数必须为严格非负整数、全部必要项就绪且采样未截断。
- 自审采样损坏 RED：缺resource_state导致KeyError、列表型state导致TypeError，2 failed/11 deselected（4.35秒）；将损坏采样明确降为unknown/media_sampling_invalid，不能将缺失信息视为不存在关键媒体。开始readiness/分类/loader/受控Chrome完整组合验证；无新增迁移、无前端菜单/路由同步。
- 完整七模块组合 GREEN：182 passed（47.28秒），两个Chrome环境均显式启用，两个DB环境键清除；包含历史v1降级/身份拒绝、新v2真实稳定窗口及损坏采样。四个新/修改媒体与测试文件Ruff全通过，git diff --check无错误。准备将仅本次7个应用/测试/文档文件原子提交，再运行全138个Web模块及隔离PG；输出助手与原始附件不夹带到实现提交。
- 原子提交 `22cbd088` 后全138个Web模块 **2230 passed/298 skipped/0 failed（409.58秒）**；隔离PG七模块 **122 passed（41.12秒）**；共享capture子集 **33 passed/336 deselected（7.76秒）**。代码冻结期间无生产或测试修改；未部署，继续处理已批准的独立分类字段缺陷。

## 分类导航事实优先级的独立最小修复

- 中断轮三个已完成菜单的正式分类分别为content_list候选0.65/0.70/0.75；都缺 `repeated_link_count>=2`，而无遮挡、primary_detail_link_count、repeated_primary_card_count及post-card token均通过。这不是首页0.70软复核，也不是预算或白名单阻断。
- 只读真实Capture验证：`dd170f7e…`、`109d0837…`、`1a545dc7…` 对应28/31/30个可信详情候选，但 repeated_link_count 全为0。节点同时 `within_navigation=true`（旧通用事实）与 `within_web_navigation=false`（Web专属事实）；详情提取器正确采用Web字段优先，分类器前置过滤却提前按旧字段continue，后续正确优先级分支不可达。Artifact摘要逐件验证一致；完整前后数据见 `assessment-media-fixed-rerun/navigation-fact-red.json` / `navigation-fact-green.json`。
- Ruling仅允许统一同一次导航判定，不降阈值、不按站点硬编码；Web false覆盖旧true，Web true排除，缺失/非布尔回退旧字段。最小RED **3 failed/3 passed（4.24秒）**，包括30个真实标记投影与真实分类器签发失败；统一前置过滤与后续计数后，分类/详情/导航四模块 **187 passed（18.45秒）**。三份真实Capture只读重算 repeated_link_count=28/31/30，与候选一致；旧持久分类结果没有回填或改写。
- 格式后四模块再次 **187 passed（18.66秒）**；曾一次误写不存在的 test_web_detail_candidate.py，pytest收集退出，未运行测试；按rg实际模块名修正为test_web_list_detail_navigation.py再验，不计入通过数。原子提交 `8de64e67` 固定4文件后，完整138模块 **2236 passed/298 skipped/0 failed（410.86秒）**，隔离PG七模块 **122 passed（40.42秒）**。实现和测试在全量期间无修改，无新增功能。
- 新鲜部署前只读预检：活动Task=0、主库head=20260912_0111、持久成年声明enabled=true/adult-v1。原第三轮根目录7个明确目标可恢复归档至 `third-round/`，没有删除任何旧证据。主代理裁定仅脱敏报告、规则、审计索引/摘要/安全采样入Git；原始成人DOM/PNG本地留存，4个公开PNG/原件JSON表示差异必须独立提示，不新增截图API功能。

## 最终新轮（固定8de64e67）

- 无build重启 `docker restart alioth-api alioth-worker` 成功；API StartedAt=`2026-09-12T16:39:05.044466392Z`、worker=`16:39:08.319043433Z`，image仍为dc6f08fd…43f7。health=ok、alembic current=20260912_0111(head)、worker持久adult enabled=true/adult-v1。
- 新登录开始 `2026-09-12T16:39:59.815198+00:00`，fresh_login=true、token_stored=false；adult来自persistent_web_configuration、scope=account_session_site_independent。前两站新Run输出 `final-media-v2-navigation-rerun/`；未修改生产或测试代码。
- heiliao Task `b769a3ce-ee96-449e-916e-acadb3c9084e` / Run `60b8901f-d1ed-46fb-844c-c31bcf178c0e` 终态review/preview，默认menu_limit=NULL，冻结deadline=`19:40:00.532887+00:00`，并非3h预算耗尽。正式首页1、正式列表菜单2、真实菜单执行2、详情预留/点击1/1但合格详情0；首页+2个打开菜单全部no_candidate/WEB_AD_CANDIDATE_NOT_FOUND，DIRECT_AD_OUTBOUND=0。
- 首页Capture `3773310e-cb33-4840-a481-a3696e14fd15` 精确confidence0.70，正式home、机器必需项全部通过、无强冲突；两个菜单Capture `8a83ca5c-32d6-44df-8125-370772b11f74` / `19c37637-64bb-4be1-b05f-a0517af84240` 均0.75正式video_list，repeated_link_count与primary_detail_link_count机器项通过。媒体v2在loaded16+unknown6、loaded41+unknown1等真实Capture中稳定通过，非关键unknown旧阻断未复现。
- 详情获取与打开验证：唯一候选URL `https://heiliao.com/archives/115173/` 与destination SHA256相同；从离屏目标经单次scroll到Capture `d41fa62a-c9c2-4369-8098-cb636068a2c0`，scroll_offset=1237、visible=true、center hit=true，然后实际点击Action `action-execution:406c45975d5dbe28c0dbf7c5b34ca5906596b61d0f3210b485e029b32303a924` completed，落地Capture `5fd81f6b-6375-4f1c-b179-0f1b707459a2` URL精确匹配且stable。没有绑定失败或旧loader拒绝。
- 最终详情分类Capture `116d87e1-18c2-4cb7-b024-0569aad3c959`：模型content_detail confidence0.65，机器缺single_subject_element_count、article_body_count、paragraph_text_length>=120及正文tokens，且repeated_cards_conflict_with_detail，正式类型为空；因此WEB_DETAIL_PAGE_TYPE_MISMATCH，不把真实点击等同于合格详情。另一菜单WEB_DETAIL_SELECTION_FAILED收窄到selector结构化判定的TypeError/ValueError被捕获，现有持久诊断无细分异常，不能凭空断言是模型格式还是平台缺陷。
- 11个unauthorized经来源Capture/epoch/manifest/动作数只读检查：7个导航中心实际hit=false，4个search/logo/危险入口审计；均未reserve，没有白名单或授权批量上限证据。4个低风险stale来源 `b06bf491-3737-4443-b239-03a9d1d9773a` / epoch24，当前详情Capture116d87e1中四个href全部缺失、匹配0/hit0，当前导航仅3个不可执行logo/search/危险项；因此不能复用旧动作。安全截断成立，但首页其余分支覆盖仍不完整，不宣称不限菜单全部完成。
- hl365 Task `140cbc68-f454-458d-8324-73d8dab659c0` / Run `0005cf2b-da7f-464e-946d-addb4cf6547c`，开始`16:52:10.982438+00:00`、冻结deadline=`19:52:10.982438+00:00`，menu_limit=NULL；正在执行，终态不得提前写入。
- 历史索引扩大到1487条记录（含重复保存位置），1476件HTTP文件与其本地manifest SHA一致，另4下载失败及7原件独立保存，所有ID均能关联Task/Run/Capture。新增的3项第二/第三轮表示差异实际为带sequence/purpose/frames/schema_version的JSON原件，对应公开PNG；这意味着旧184/410“摘要通过”只证明下载端manifest一致，不可解释成全部与DB原件一致。最终索引同时记录两种摘要与额外原件路径，不隐藏差异。

## 最终终态、附件与自审收口

- hl365在 `2026-09-12T17:11:27.733048+00:00` failed/sealed，CLI正常结束时间 `17:11:29.394152+00:00`，没有本轮Docker中断。正式首页1（confidence0.80）、菜单1（content_list/confidence0.85），真实菜单动作1，详情预留/点击0/0；首页与菜单广告均observed。剩余12项由WEB_DISCOVERY_UNKNOWN_OUTCOME_BLOCKED阻断，另2项unauthorized、3项cross-origin；不是数量预算耗尽。
- 最新菜单的正常重复链接/详情入口/主卡片机器项全部通过；主体补图有界scroll两次后 `WEB_PRIMARY_CONTENT_AFTER_UNTRUSTED`，菜单 `WEB_MENU_POST_NAVIGATION_FAILED` / RuntimeError，交接 `WEB_NAVIGATION_CURRENT_UNTRUSTED`。末Capture `2d189538-689b-40b4-ba62-617f48e0cd29` 的稳定窗口104次采样、stable_sample_count=0、quorum=false、sample DOM quiet7.6ms<600、critical_network_pending=9，动态原因dom_continues_changing/background_requests_pending。最终采集瞬间虽quiet737.5ms/pending0/必要媒体3/3loaded、无loader/transition，却不能替代连续窗口；2.494秒采集前后结构摘要f82d…→2896…不一致，capture_consistent=false。URL/context/page/document epoch/visibility/top-layer均相同，唯一允许缺口account_identity；不是媒体v2再次误阻断，也不能据此排除全部外站动态性/平台适应性concern。
- 两页广告持久logical_page_id唯一，trace `e284d0c2-b1a8-493f-8274-77bd40b025bf` / `1d9d0dd9-bbd6-4cb5-86a6-e6d21423d4c1` 原件SHA256通过，各DIRECT_AD_OUTBOUND=1、new_page_count=1、closed_after_observation_count=1、source_state_change_count=0。普通清障7次click与广告取证分离，没有重复外跳。
- 第三站保持Task `bea3b3d6-cf42-4658-9142-b94e8dc02146` / Run `680e8232-5435-4f36-8363-984f165d3f2c` 的failed/sealed与COMMAND_HOST_BOUNDARY_VIOLATION；15件边界附件已保全。前两站最终新Run与第三站保留轮次明确记录在 `execution-provenance.json`，single_execution_round=false，不恢复第三站旧Run或访问跳转主机。
- 最终两站335件HTTP附件全部保存，宿主 `Get-FileHash -Algorithm SHA256` 逐件335/335与manifest一致，88,961,001 bytes。其中333件也与数据库原件一致；2件广告paired screenshot仅下载了公开首帧PNG，索引已另存数据库JSON原件（Artifact01fc3ee3…与9ac76904…），没有把它们误称HTTP原件下载成功。全轮次索引1824条记录/1625个唯一Artifact，1811个保存位置、4个原下载失败记录、9份独立原件；原件只保留本地，原始成人内容不入Git。
- 三站脱敏 `final-exploration-report.json/.md/.html` 已生成：passed=false，117项聚合检查未满足，不代表117个独立缺陷；主要concern为首站低置信/结构冲突详情与未覆盖分支、结构化选择失败无细分异常、第二站动态稳定性/结构漂移、第三站跨主机边界、公开附件表示差异，以及未做人工作台复核。所有事实与证据ID保留，没有伪称详情合格、没有伪造no_candidate、没有用review放宽成功门禁。
- 自审：内存摘要已携带result.ad_landing_evidence；reserve_detail注解包含WebRuntimeMenuEntry；必要的预存lineage/ad evidence集成已在b5041376及后续原子提交，未遗漏依赖。当前生产/测试代码自8de64e67后无变动，最终文档只修正历史规则兼容边界及证据摘要口径。不再追加实现功能。
- 提交边界：仅本Task报告、专项规则与outputs内9份显式安全文件（含.gitignore）；原始DOM/PNG/完整CLI响应及临时助手排除。既有 `services/api/app/domain/page/__init__.py`、`nav_alias.py`、`test_page_nav_alias.py` 和三个task8输出目录保持原状、不提交。
- 最终安全文件核验：9份白名单文件通过已知登录秘密/JWT模式扫描，全部JSON解析成功；原件宿主SHA256 9/9与数据库相符，最终报告两页广告全部Artifact ID存在索引、DIRECT/new/close=1/1/1、源页change=0。`git diff --check`无空白错误；原有全仓库lint历史问题未借此宣称已全部修复。

## Not Ready 审查四项集中修复（2026-09-13）

本轮仅处理广告出站、DOM完整性、来源身份和原件导出四项。按系统调试/TDD/验证后声明技能执行，未委派子代理。主代理批准广告专用隔离context与固定IP传输，并明确禁JavaScript的静态证据边界；不改普通站点导航、数据库结构或前端菜单/路由。此前“不新增截图API”仅约束历史实况收口，本轮审查明确授权原件表示导出，因此增加同一鉴权端点的original模式，未新建旁路下载接口。

### 根因、RED与最小修复

- P1：候选/直开仅验证HTTP(S)和userinfo，实际context.new_page.goto重新解析目标且自动跟随重定向。静态RED 11 failed/2 passed，覆盖回环、私网、链路本地、多播、保留地址、IPv4映射及畸形IPv6。共享URL策略后13 passed；传输模块不存在的RED为11 failed/13 passed，受控本地目标GREEN为24 passed。新增HTTPS Location逐跳负例、真实Chromium隔离后纳入最终组合。生产连接只接收已验证公网IP，TLS仍用原host验证证书；每跳全部DNS地址均须公网，同host地址集合变化拒绝；手动重定向不把未检查Location交给浏览器。测试连接接缝仅在用例中把已验证地址接到本地HTTP fixture，绝未访问元数据或其它真实目标。
- 广告渲染隔离：新context不继承cookie/opener，禁脚本、service worker、WebSocket及非HTTP网络能力；CSP允许静态图文/样式/字体与无外联data图片。所有网络经route.fulfill，失败只abort，不使用代理链/环境代理/直连回退；不可用loopback代理作为漏网兜底。DNS/连接单次最多3秒、观察最多30秒（实际沿用较小动作预算）、单并发/64请求/5跳/8MiB单响应/32MiB累计；总时限到达主动中断socket，避免慢响应头续期。证据明确 `isolated_safe_transport/static_render`，不是完整动态渲染。无可见正文或已解码图片的脚本壳单独RED 1 failed/8 passed，修后不足而不是observed。
- P2/P3联合RED 7 failed/1 passed：3个page.content异常/空白被误报observed，3个query/hash/current_url_changed漂移被误报回位，1个证据不足被混同回位失败。GREEN 8 passed；DOM状态随URL/截图持久化，空/不可读/静态内容不足只生成evidence_insufficient，phase不能completed。来源使用内部完整scheme/authority/path/query/fragment比较，current_url_changed=true独立拒绝；输出仍脱敏。回位成功与证据完整分开，失败领取不重试。
- P4端点RED 2 failed/3 passed，真实FastAPI+TaskInsightService原件请求仍收到首帧PNG；新增 `representation=original` 后返回未经转换的原存储字节/原SHA256，保留preview首帧、票据主体和对象撤权复验、先审计后外发。CLI只认可同API精确票据路径、单ticket参数、禁止跳转，核对原件响应摘要及已有清单后保存；共用ID的双帧JSON保存一次，不落盘凭据/票据。
- P4真实PG自审发现原查询未投影checksum：新增真实隔离PG仓储→真实FastAPI端点回归，RED HTTP409（1 failed/7 deselected，4.41秒）；仅补SELECT artifact.checksum，GREEN随下列130项组合通过。无迁移。端点负例覆盖无票据404、损坏摘要409、已停用用户旧票据404；原件双帧及默认预览均验证。没有用mock endpoint代替集成测试。

### 本轮新鲜验证

- 相关六模块命令：`docker exec -e PYTHONPATH=/workspace alioth-api python -m pytest services/api/tests/test_web_ad_outbound_security.py services/api/tests/test_web_ad_evidence_integrity.py services/api/tests/test_web_ad_landing_evidence.py services/api/tests/test_web_live_artifact_export.py services/api/tests/test_evidence_download_authorization.py services/api/tests/test_ad_landing_evidence.py -q --tb=short -p no:cacheprovider`：102 passed，26.51秒。真实Chromium证明内网子资源未连接、来源cookie/opener隔离、脚本不执行、context观察后关闭；普通清障回归仍绿。
- 全Web命令由容器Python枚举 `Path('services/api/tests').glob('test_web*.py')` 后执行同一pytest参数；进程先移除ALIOTH_DATABASE_URL/ALIOTH_TEST_DATABASE_URL，显式指定已有Chromium。共140模块（原138+本轮2）：2278 passed、299 skipped、0 failed，426.51秒。跳过项含显式PG环境门；没有宣称这些跳过项已由普通全量覆盖。
- 隔离PG命令在容器内将测试DSN仅替换path为 `/alioth_test_unbounded_menu`，保留原主DSN只供隔离身份比较；不输出任何DSN。原预算迁移/仓储/PG并发/discovery/formal-report-E2E/authorization/claim七模块122项，加 `test_web_live_artifact_export.py` 八项，共130 passed、0 failed，42.85秒。原122项全部通过；真实PG原件端点已覆盖。
- 15个本轮修改/新增Python文件Ruff通过；git diff --check通过。少量同文件既有长行/导入排序仅机械排版，无行为改变。全量测试期间只做此类排版及原件仓储checksum投影的独立PG验证，不改导航/分类或实况结果。提交前最后同一六模块再次102 passed/1 skipped（仅PG端点环境门，已在130项隔离组合通过）、0 failed，26.39秒；Ruff再次All checks passed，diff-check退出0。
- 只读 `alembic current` 仍为20260912_0111(head)；API/worker仍running，StartedAt分别16:39:05.044466392Z/16:39:08.319043433Z，image仍dc6f08fd…43f7。本轮未restart、未build、未取新token、未发新Run、未修改旧Run或主库。

### 离线原件回放与结论边界

- 两页历史广告trace `e284d0c2-b1a8-493f-8274-77bd40b025bf` SHA256=`0955434252a97d315f9a970789a4343571085047669428900937f9250d8bfca7`，`1d9d0dd9-bbd6-4cb5-86a6-e6d21423d4c1` SHA256=`0a53efb08707284d8ca97b694bdb2c909d636a41dedd94b90aaf064937873ae1`；原件哈希与索引一致，各DIRECT/new/close=1/1/1、完整URL变化标志false。
- 落地DOM原件 `b56d6595-bcde-4903-9682-a3dfd5674895` / `c1dde594-0fae-4116-9479-53237ce293de` 均非空，SHA256同为 `1fc8646f1b0b166c607adf2eaf430f28d7300232405dbd5c571bb9cc98b7f785`。两份paired screenshot原JSON（01fc3ee3…/9ac76904…）仍各含2帧，原SHA一致；历史首帧PNG表示差异记录不删除、不伪改为原件HTTP下载成功。
- 原始文件路径仍以 `outputs/web-live-20260912-three-sites/evidence/local-artifact-index.json` 的绝对/相对位置为准；原始成人DOM/PNG不入Git。本轮离线回放只输出ID、计数、状态和摘要，未输出原文或敏感值。
- 三站历史结果不改变：heiliao review（详情确已点击但未合格）、hl365 failed（动态稳定/结构漂移）、第三站跨主机边界failed。本轮不重跑三站；历史observed只证明当时取证事实，不能追溯认证旧DNS/出站安全，也不能证明新的静态渲染策略已经过实站验收。脚本依赖广告的静态呈现能力、严格DNS集合变化对CDN可用性的影响是明确concern，安全拒绝不放松。
- 自审提交边界：只提交本轮四项实现、回归、规则和本报告；不触碰未提交nav_alias/__init__/test_page_nav_alias与task8三个输出目录。没有新增依赖、迁移、服务重启或站点行为；等待本轮复审，不把测试通过改写成三站业务通过。

## 复审两项 Important 集中修复（2026-09-13）

本轮只修隔离广告桌面画像、来源context新页审计两个问题，基于上一轮提交 `374ead98`。按系统调试、TDD与验证后声明技能执行，未委派；没有新增依赖、数据库迁移、前端菜单/路由同步、服务重启、部署或实站重跑。原三站历史结论及静态渲染未获实站验收的concern不变。

### 根因与真实 Chromium RED/GREEN

- 桌面画像：隔离context未设置viewport/device_scale，Playwright默认1280×720被源页1920×1080的截图合同拒绝。新增真实Chrome→`WebAdLandingCaptureAdapter`→`WebDesktopCapturePolicyBrowser`→`PageCapturePersistenceService`→`LocalEvidenceArtifactStore`完整回归，未mock压缩器/原件存储；元数据仓储与一次性claim使用受控实现。RED为1 failed/30 deselected（7.06秒），栈从adapter经过桌面策略 `_compress_screenshot_node` 抛出 `WebScreenshotContractError`，明确实际1280×720。仅复用 `WebBrowserProfileRegistry.resolve(DEFAULT_WEB_BROWSER_PROFILE_ID)`，显式传入viewport/screen/device_scale/mobile/touch/color参数；同命令GREEN 1 passed/30 deselected（5.10秒）。两帧均1920×1080、`png_lossless_optimize`，持久原件SHA与返回元数据一致，不复制生产魔数。
- 来源context：`direct_observation or await _record_navigation_outcome(...)` 在隔离广告已返回时跳过来源页面审计与关闭。受控真实Chrome在广告transport执行期间用源页 `setTimeout(window.open('about:blank'), 10)` 产生额外页，不访问外站。RED为1 failed/31 deselected（7.11秒），捕获返回后来源context仍2页。修复后始终进入审计/关闭，已有隔离观察作为参数传入，来源新页只审计和关闭，不再观察广告、不等待其加载、不跟随。
- 两项一起GREEN：`test_web_ad_outbound_security.py -k 'real_source_timer or real_ad_adapter_persists' -q --tb=short -p no:cacheprovider` 为2 passed/30 deselected（7.23秒）。污染用例断言来源只剩原页、额外页已closed、总new/closed=2/2、isolated=1、source_context=1、`source_context_contaminated=true`，来源URL不变仍 `source_restored=false`，持久phase=failed/WEB_AD_LANDING_RETURN_FAILED。原隔离双帧及落地定位仍保全；DIRECT动作、实际受控广告HTTP请求及claim.finish各1次，再次领取被拒绝。普通弹窗清障路径不改。

### 组合验证与边界

- 相关七模块：`test_web_ad_outbound_security.py test_web_ad_evidence_integrity.py test_web_ad_landing_evidence.py test_web_desktop_capture_policy.py test_ad_landing_evidence.py test_page_capture_persistence.py test_page_capture_tool.py`，统一 `python -m pytest ... -q --tb=short -p no:cacheprovider`，508 passed/1 failed（169.08秒）。不能将该补充组合宣称全绿。
- 唯一失败为既有 `test_page_capture_persistence.py::test_postgres_repository_persists_web_evidence_admission_without_migration`。清除 `ALIOTH_DATABASE_URL` 和 `ALIOTH_TEST_DATABASE_URL` 后单独重现1 failed（4.21秒），排除环境污染。最窄边界：该FakePool夹具提供 `metrics.web_readiness`，但没有冻结Web policy；仓储进入Web证据准入分支后从FakePool读取None，在 `page_capture_repository.py:147` 以 `WEB_POLICY_HISTORY_MISSING` 拒绝。相邻不带web_readiness的夹具不进入此分支。测试文件与仓储文件对 `374ead98` 的git diff均为空，未改本轮无关旧夹具或生产合同；按主代理限定，仅记录风险。该文件不属于140个 `test_web*.py`。
- 隔离PG沿用上一轮八模块，唯一测试数据库 `alioth_test_unbounded_menu`，不输出DSN；新鲜结果130 passed/0 failed（42.62秒），包含持久claim并发/生命周期与真实PG→FastAPI原件端点。
- 全Web140模块使用显式已安装Chromium、移除两个DB环境键，枚举 `test_web*.py` 后执行 `python -u -m pytest ... -q --tb=short -p no:cacheprovider --junitxml=/tmp/task7-second-review-web140.xml`：**2280 passed/299 skipped/0 failed，428.87秒**。JUnit只读核验tests=2579、failures=0、errors=0、skipped=299，SHA256=`84db9dcfe5bd2ac267f610d27763a293544f412d014dd35b977a22fa1801d687`。跳过项中的PG门由独立130项验证补充，不声称所有跳过均执行。第一次全量工具会话输出在上下文压缩时丢失，不据此声明通过；第二次等待原进程结束后顺序运行并固定上述XML，没有并发重跑同套测试。
- 最后静态检查：4个本轮Python文件 `python -m ruff check ...` 为All checks passed，`git diff --check`退出0。全量验证期间未修改生产或测试逻辑，仅整理本报告；上一段补充组合的旧夹具失败仍保留，未用全Web通过隐藏它。
- 自审范围：三个生产文件、一个回归文件、专项规则及本报告；不触碰nav_alias/__init__/test_page_nav_alias、Task8输出。已有安全transport、禁JavaScript/SW/WebSocket和漏网代理兜底不变。新增来源污染计数不覆盖原隔离计数，关闭失败仍由既有close_failed审计与回位失败约束；真实回归用受控about:blank，不创建第二份广告证据。

## 最终整分支审查：唯一修复波次（2026-09-13）

### Ruling 与范围

- 主代理裁定本轮仅处理正式页型绝对0.70门槛及广告累计32MiB流式额度；不新增功能、不修改nav_alias/Task8，不部署、不重跑三站。已有三站未全通过结论不变，没有重新认证历史分类/广告证据。
- 所有Web正式页型的原始confidence必须至少0.70；显式minimum_confidence仅可收紧，低参数向上钳制到全局下限而不是改变原分。可信列表→详情、DOM佐证及历史提示只保留候选/审计，不能使0.60/0.65/0.699正式化。正式签发和Recorder直接调用均再次防御，低分按既有needs_review/evidence_insufficient合同持久化，不授予inspection角色；达到0.70仍不得绕过结构必需项或强负信号。
- 32MiB是一次广告观察的累计应用读取上限，剩余额度必须传入流读取；下一请求在DNS/连接前检查，每个chunk立即消费，失败不能退款。未知长度在边界无法证明EOF时失败关闭，不能多读取探测字节。已知定长EOF可以恰好用完额度正常完成，但不能在已关闭的socket上再设超时。

### 系统调试与 TDD

- P1根因：`WebPageClassifier`允许任意0–1显式minimum；corroborated分支另设0.65/可信详情0.60，并在最终比较中以`and not corroborated`旁路。Recorder只检查非空页型/闭集/复核原因，不检查原分；RecordingClassifier也直接把该结果返回。
- 最小RED覆盖五类×0.699/0.70、显式minimum=0、0.80更严门槛、可信详情0.60/0.65/0.699/0.70、五类Recorder直接调用及RecordingClassifier签发。首轮视频正夹具遗漏player_root事实导致两个结构拒绝，按机器合同补齐夹具，不修改机器规则。补齐后分类部分24 failed/16 passed；低分仍正式、Recorder仍classified及0.80被佐证绕过均稳定复现。旧 `test_detail_dom_corroboration` 改为保留候选和原分，低于0.70只能复核。
- P1最小实现删除0.65/0.60例外、minimum钳制全局下限、corroborated也须达到有效门槛、末尾置信度比较不再有例外；签发/Recorder复用同一出口防御，保留原confidence/raw/proposed/evidence并生成稳定低分原因码。完整分类与Recorder两模块GREEN **91 passed，5.78秒**。
- Minor根因：`_fetch_pinned`只知道单响应8MiB，`AdSafeTransport`在完整响应返回后才累加总量。真实本地未知长度连续响应RED为 **1 failed/33 deselected，4.63秒**：5×6MiB后又读取8MiB，实际39,845,888 bytes（38MiB）才拒绝，应在33,554,432 bytes（32MiB）停止。
- 定长正例另揭示同一读循环在HTTPResponse完成并关闭文件后仍调用sock.settimeout，原栈为 `OSError: [Errno 9] Bad file descriptor`，被包装成NETWORK_FAILED；已通过临时测试诊断接缝取得完整栈并移除该接缝。修复读循环先检查EOF，再按单响应/累计剩余最小值读取；每块通过单并发消费回调立即累计，异常也不退还。真实4×8MiB定长响应可完成，下一请求不发；5×6MiB后未知流最多再读2MiB即失败，后续请求仍拒绝。完整广告安全模块GREEN **34 passed，17.50秒**，含既有真实Chromium隔离双帧、来源污染、安全出站回归。

### 固定实现验证与提交边界

- 六模块聚焦命令：`test_web_page_classification.py test_web_page_type_assessment.py test_web_ad_outbound_security.py test_web_ad_evidence_integrity.py test_web_ad_landing_evidence.py test_web_desktop_capture_policy.py`，清除两个DB环境键，执行 `python -m pytest ... -q --tb=short -p no:cacheprovider`：**169 passed，31.57秒**。
- 全Web首轮为2320 passed/299 skipped/1 failed（431.50秒），固定失败JUnit `/tmp/task7-final-floor-budget-web140.xml`。唯一失败 `test_web_runtime_composition::test_home_menu_and_detail_classification_share_one_page_type_recorder` 的替身仅返回带page_type的SimpleNamespace，缺真实WebPageAssessment合同中的confidence；新签发防御因此AttributeError。清除DB环境单独复现1 failed（5.35秒）后，仅将该替身改为真实WebPageAssessment(confidence=0.9,evidence完整)，没有给生产出口加缺字段默认值/绕过。组合根+分类+Recorder完整三模块GREEN167 passed（9.66秒）。该夹具与本轮签发合同直接相关，不是上一轮无关FakePool夹具。
- 隔离PG八模块130 passed/0 failed（42.98秒），只使用alioth_test_unbounded_menu；JUnit `/tmp/task7-final-floor-budget-pg130.xml` 的tests=130/failures=0/errors=0/skipped=0，SHA256=`f3f8dc56684dcea0b07198db6f3831f6c02277a6d43000fa85e3f7d2b0f3b41d`。PG通过后只调整组合测试替身，生产逻辑保持固定。
- 本轮全部7个Python文件最终Ruff通过，git diff --check退出0；同文件既有导入顺序/长行仅机械排版，不改变行为。修正直接相关夹具后全Web140重跑 **2321 passed/299 skipped/0 failed，429.47秒**，显式启用已安装Chromium、清除两个DB环境键。JUnit另存 `/tmp/task7-final-floor-budget-web140-rerun.xml`，只读核验tests=2620/failures=0/errors=0/skipped=299，SHA256=`cda26ea0bfae03b98cb5802e985c81e93d9edd70540eeac5830ccce533a654b6`；保留首轮失败原件，没有以选择性通过替代完整重跑。跳过项含PG门，隔离130项另已验证，未宣称所有跳过项实际执行。
- 本轮仅三个生产文件、四个测试文件、专项规则与本报告。无新增依赖、迁移或前端菜单/路由同步；没有修补上一轮补充组合的无关FakePool缺冻结policy夹具。原始成人附件/登录秘密不参与本轮测试或提交。
