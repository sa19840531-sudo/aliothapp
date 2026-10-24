# 本轮 Web 验证环境

## T10 独立服务元数据预检（2026-09-11，主控只读）

- 仅Docker `ps --filter name=alioth` 与指定容器的非敏感 `inspect --format`，每次12秒进程上限；首次普通沙箱启动Docker子进程被WinError5拒绝，随后只读授权检查成功。没有读取完整环境变量、连接PG或变更服务。
- 当前 `alioth-api` 和 `alioth-worker` 均使用现成镜像 `alioth-api:local`，工作目录 `/workspace`，挂载目标 `/workspace` 为rw。与镜像是否包含最新源/主库迁移是否完成是不同问题，不能由容器存活推断新功能已部署。
- 当前主API仅本地8000，Web5173、Redis6379、主PG5432；本轮专用PG为仅本地55439，容器仍在运行。该预检不执行任何重启/迁移/新Run，也不占用实现者PG会话。
- Compose源码确认API入口为 `uvicorn services.api.app.main:app`，Worker为 `python -m services.api.app.worker`。可供后续独立服务复用镜像；实际启动仍须隔离数据库、队列、附件目录及绑定端口，不复用全平台initialize或整套主环境变量。

## 隔离边界

- 原有 Alioth PostgreSQL 位于本机 5432，API/Worker 挂载当前仓库。本轮不得以主库代替测试库；未主动重启主服务。
- 后续只读检查发现实际 API 命令包含 `--reload --reload-dir /workspace/services/api/app`：Web 源码编辑也可能触发 API 自动重载，不能声称共享服务进程完全不受影响。检查时非 Web queued/running 任务数量为 0（只汇总数量，不读取任务内容、不写主库）；本轮不修改共享 compose 或其他端代码来规避重载。
- T3 复修期间只读访问 `GET http://127.0.0.1:8000/api/v1/health` 返回 200、status=ok；没有触发任务。存活检查不代表模型/浏览器/Worker 已更新或六站环境就绪，后续仍需独立验证。
- 本轮临时 PostgreSQL 容器：`alioth-web-gap-test-20260909`，镜像 `pgvector/pgvector:pg16`，仅监听 `127.0.0.1:55439`，数据目录使用 tmpfs，不挂载主库卷。
- 数据库 `alioth_test_web_v2`，角色 `alioth_web_gap`；密码只在测试进程环境中传递，不写仓库配置。
- 已实查连接身份，完成空库升级至 `20260908_0099`，现有 Web 预算 PostgreSQL 集成用例 7 项通过。该结果不代表后续新增迁移已经通过。
- PostgreSQL 测试必须显式指定测试连接，执行项目的隔离检查并核对实际数据库/角色；不允许缺失环境变量后回退 `.env` 主库。
- Docker 位于 `C:/Users/p/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe`，不在默认命令搜索路径中。

## 离线 Web 基线命令

在仓库根目录执行，不注入 PostgreSQL 测试环境变量：

```powershell
$webBaselineTests = @(Get-ChildItem -LiteralPath 'services/api/tests' -Filter 'test_web_*.py' | ForEach-Object { $_.FullName })
& '.venv/Scripts/python.exe' -B -m pytest @webBaselineTests -q -p no:cacheprovider
```

初始结果：1203 passed / 49 skipped / 4 warnings。部分测试存在导入/收集顺序依赖，后续阶段统一保留该文件顺序用于基线比较；跳过的 PostgreSQL/浏览器用例必须另行实跑，不能记为通过。全仓 `-k web` 仍会收集无关 App 文件，本轮不修改 App 来绕过收集失败。

## 浏览器与前端

- 已安装 Chrome：`C:/Program Files/Google/Chrome/Application/chrome.exe`。局部 Web HTML 测试显式使用该程序，不修改其他端浏览器配置。
- 默认 Playwright Chromium 未安装。官方 CDN 安装尝试已取消，不视为安装成功。
- T4 期间另试仅安装 headless shell（项目当前 Playwright v1234 对应 Chrome Headless Shell 151），独立 PLAYWRIGHT_BROWSERS_PATH 临时目录、未改依赖；官方下载超过 5 分钟仍为零字节，已停止本轮安装进程（退出 1），未记为安装成功。后续优先通过仅 Web 测试级的 Chrome 启动适配实跑；不要为了消除 skip 改 H5/App 默认或全局测试 fixture。
- 中止后经提权只读 CIM 核查，未发现命令行匹配本轮 `playwright install --only-shell --no-progress` 的 Node/Python 安装进程；没有终止其他进程或清理其他下载目录。
- 前端基线：任务列表、任务详情、报告中心、报告详情四个测试文件共 39 项通过；`npm run build` 通过。
- 真实站点回归安排在开发和代码审查之后；受控 HTML 浏览器测试不能代替六站结果与人工报告复核。
- Worker 镜像额外预检：`alioth-worker` 内 Playwright 1.62.0，自带 Chromium 可执行文件存在。首次仅检查路径的同步驱动提前关闭产生 pending-task 警告；随后独立异步上下文真实启动 Chromium、打开 `about:blank` 并关闭，输出 `chromium_blank_page_ready=True`、退出 0。未访问外站、触发任务、写库或重启 Worker。容器内浏览器可用与 Windows 默认 Chromium 缺失是两个环境，不应混为同一个阻断；这仍不代表模型或新版 Worker 业务代码已就绪。

## 共享边界预检

T3 开发期间执行如下只读测试，不属于 T10 最终验收；后续共享 Web 分支变更后重跑。结果为 176 passed / 2 既有框架 warnings，2.89s，未改共享实现和测试。

```powershell
& '.venv/Scripts/python.exe' -B -m pytest services/api/tests/test_capture_coordinator.py services/api/tests/test_evidence_input_set.py services/api/tests/test_evidence_input_set_repository.py services/api/tests/test_page_graph.py services/api/tests/test_page_graph_projection.py services/api/tests/test_page_graph_progress.py services/api/tests/test_evidence_completeness.py services/api/tests/test_observation_packet.py services/api/tests/test_observation_packet_repository.py services/api/tests/test_observation_packet_presentation.py services/api/tests/test_surface_session_repository.py -q -p no:cacheprovider
```

T4 开始前追加工厂/指南相关基线，4 文件 232 passed / 2 既有 warnings，2.44s。不是 T4 后验收，不能替代后续重跑。

```powershell
& '.venv/Scripts/python.exe' -B -m pytest services/api/tests/test_worker_web_v2_route.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_skills_loader.py services/api/tests/test_surface_driver_adapter.py -q -p no:cacheprovider
```

## A 阶段审查后新鲜回归

T1～T3 独立审查均通过后，主控再次按上文全 Web 命令执行：1290 passed / 49 skipped / 4 warnings，32.43s，退出 0。跳过与既有框架警告继续单列，不代替六站。

上文共享边界两组共 15 文件合并执行一次：408 passed / 2 warnings，5.58s，退出 0；不是把历史各轮结果累加。未对这些共享/H5 测试或实现作改动。

## T4 期间 Web 浏览器独立预检

对 `test_web_list_detail_navigation.py` 与 `test_web_obstruction_snapshot.py` 执行以下独立测试进程：85 passed / 0 skipped / 2 既有 warnings，18.61s，退出 0。包含真实受控 HTML 的详情候选、清障候选与年龄提示层快照验证；不是六站探索，也不是 T4 最终验收。没有编辑测试/产品文件或 H5/App 配置。

```powershell
$env:PYTHONUTF8='1'
@'
import pytest
from playwright.async_api import BrowserType
original_launch = BrowserType.launch
async def launch_local_chrome(self, *args, **kwargs):
    if self.name == 'chromium' and not kwargs.get('executable_path'):
        kwargs['executable_path'] = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
    return await original_launch(self, *args, **kwargs)
BrowserType.launch = launch_local_chrome
raise SystemExit(pytest.main(['services/api/tests/test_web_list_detail_navigation.py', 'services/api/tests/test_web_obstruction_snapshot.py', '-q', '-rs', '-p', 'no:cacheprovider']))
'@ | & '.venv/Scripts/python.exe' -B -
```

复用同一进程级 Chrome 适配，改 pytest 参数为 `test_web_capture_runtime.py -k "test_real_playwright_request_clones_preserve_committed_click_identity or test_real_visual_postclick_failures_are_idempotent_and_clear_private_frame" -q -rs -p no:cacheprovider`：5 passed / 0 skipped / 284 deselected / 2 既有 warnings，18.05s，退出 0。真实请求克隆与点击后失败幂等性通过；该文件中的共享行为只作回归，未修改 H5/App。

## T4 交审后的主控核验

只读访问本轮隔离容器内数据库，实查返回 `alioth_test_web_v2 | alioth_web_gap | 20260909_0100 | qc_web_run_policy_snapshot`；与实现者报告最终迁移状态一致，不代表主库已部署。

为 TaskInsight 共享入口补跑 `test_task_insight_api.py` 和 `test_workstation_reports_api.py`：29 passed / 4 既有 warnings，2.60s，退出 0。命令为 `.venv/Scripts/python.exe -B -m pytest services/api/tests/test_task_insight_api.py services/api/tests/test_workstation_reports_api.py -q -p no:cacheprovider`。未改共享报告测试/业务；此项不是 T10 最终报告和六站验收。

## T5 前共享消费者边界基线

T4 复审 Approved 后、T5 实现者刚开始阅读技能时，主控确认 semantic_target_resolver、h5_live_inspection_publish、web_completion_gate_sql、web_coverage_snapshot_repository 四个生产文件尚无 dirty 差异。执行以下组合基线：**121 passed / 2 既有 warnings，1.37s，退出 0**，不作为 T5 完成后的验收。

```powershell
& '.venv/Scripts/python.exe' -B -m pytest services/api/tests/test_web_semantic_target_resolver.py services/api/tests/test_h5_live_inspection_publish.py services/api/tests/test_web_live_inspection_publish.py services/api/tests/test_web_coverage_snapshot_repository.py services/api/tests/test_task_completion_gate.py -q -p no:cacheprovider
```

同阶段运行 `test_h5_secret_input_runtime.py` 与 `test_h5_secret_input_coordination.py`，28 passed / 2 既有 warnings，0.51s，退出 0；两者也使用共享 SemanticInteractionTargetResolver，作为 T5 Web 分支注入后的 H5 不变性基线，未修改其代码/测试。

## T6 前真实页面图归属基线

T5 实现前期，主控在显式双 DSN、项目隔离守卫及实际数据库/角色均核验后运行 `test_page_graph_run_scope_postgres.py`：**8 passed / 2 既有 warnings，2.76s，退出 0**。只使用本轮 55439 数据库，既有迁移 fixture 检查当前 head，随机夹具按 ID 清理；未改 App 测试帮助器、共享页面图实现或数据库约束，未连接主库。与 T5 实现者事先协调，执行已结束。不是 T6 新 Web 分类投影链路的验收。

核心命令：`.venv/Scripts/python.exe -B -m pytest services/api/tests/test_page_graph_run_scope_postgres.py -q -p no:cacheprovider`。执行前必须按本文配置本轮测试 DSN 和守卫比较 DSN，并连接核对 current_database/current_user，不允许缺省回退。

## T5 开发期间补充 App 不变性基线

主控只运行四个既有 App 证据/状态/图纯测试文件，无生产或测试文件修改、无数据库/浏览器调用：**29 passed / 2 既有 warnings，0.69s，退出 0**。这是共享修改前的补充边界基线，不是 App 全量或 T10 完成验收。

```powershell
& '.venv/Scripts/python.exe' -B -m pytest services/api/tests/test_app_surface_evidence.py services/api/tests/test_app_capture_state_invariants.py services/api/tests/test_app_graph_recapture.py services/api/tests/test_app_evidence_index.py -q -p no:cacheprovider
```

## T5 工作台聚合接线前的中间回归

主控发现任务时间线/报告中心还有两个独立消费者后，分别运行既有共享服务测试，无文件修改：

- `test_report_insight.py`：37 passed / 2 failed / 2 既有 warnings，0.52s。
- `test_task_insight_service.py`：77 passed / 2 failed / 2 既有 warnings，0.53s。

两文件各自失败的均为 Web 场景：没有可信 alias 的 HTTP/HTTPS 仍期望合并，以及 `/landing` 仅凭评估里的 home 声明仍期望首页。T5 新 URL 语义已经生效，但实际工作台读端当时仍需冻结身份接线；已将实测告知实现者，要求同时补可信映射正向与缺失/冲突负向，不能恢复无条件合并或仅把全部断言改绿。上述结果不是 T5 最终验收，后续须整文件复跑；不能因 `test_web_*.py` 全绿而遗漏这两文件。

命令分别为 `.venv/Scripts/python.exe -B -m pytest services/api/tests/test_report_insight.py -q -p no:cacheprovider` 与 `.venv/Scripts/python.exe -B -m pytest services/api/tests/test_task_insight_service.py -q -p no:cacheprovider`。

## T5 交审后的主控核验

独立于实施者的 16 文件组合，主控重跑前述 15 文件共享/Worker/H5 边界：**408 passed / 2 既有 warnings，5.40s，退出 0**。包括 Coordinator、InputSet 领域/仓储、页面图领域/投影/进度、齐全性、Observation Packet 领域/仓储/呈现、Surface Session、Worker Web 路由、H5 装配、Skill loader、Surface driver；无文件修改。不能累加与实施者重叠的测试作为独立总数，也不能覆盖审查者新发现的 Web 晚重复 bundle 缺口。

本轮隔离容器只读 SQL 实查返回：`alioth_test_web_v2 | alioth_web_gap | 20260909_0101 | qc_web_run_policy_snapshot | qc_web_logical_target | qc_web_logical_target_alias`。符合本轮库身份和迁移状态，未连接主库或执行迁移。

## T6 实现开始前的 2026-09-10 主控基线

原 T5 复审 Approved 后、T6 新实现者开始读取说明时，主控独立运行以下 9 文件纯测试组合：**138 passed / 2 既有 warnings，2.39s，退出 0**。没有配置或连接 PG，没有修改生产/测试。这是共享图及 H5/App 边界的起点，不是 T6 新投影链的验收。

```powershell
& '.venv/Scripts/python.exe' -B -m pytest services/api/tests/test_page_graph.py services/api/tests/test_page_graph_projection.py services/api/tests/test_page_graph_progress.py services/api/tests/test_capture_coordinator.py services/api/tests/test_web_page_type_assessment.py services/api/tests/test_app_surface_evidence.py services/api/tests/test_app_capture_state_invariants.py services/api/tests/test_app_graph_recapture.py services/api/tests/test_app_evidence_index.py -q -p no:cacheprovider
```

## T6 的 Docker 执行权限检查

T6 启动时主控另对明确的本轮隔离容器执行一次只读 Docker 检查：在 `exec_command` 的 `require_escalated` 权限复核下，用同一 Python 进程 subprocess 捕获 `docker inspect --format '{{.State.Running}}|{{.Config.Image}}' alioth-web-gap-test-20260909`，返回 `true|pgvector/pgvector:pg16`、退出 0。默认沙箱的 WinError 5 不代表 Docker 已损坏；不要静默反复默认重试。该检查未取口令、未连接 PG、未迁移，也不追溯改写 T5 复审“未独立运行 PG”的历史。

## 清理

### T6 交审前主控新鲜回归（2026-09-10）

实现者声明实现停止、仅整理报告后，主控独立按本文原 Get-ChildItem 绝对路径顺序执行全体 test_web_*.py：**1377 passed / 106 skipped / 4 既有 warnings，41.39s，退出 0**。未设置 PG 测试 DSN；106 跳过含本项新增的 35 个 PG 用例，它们由实现者隔离 PG 组另行实跑，不算本命令通过。较 T5 广回归新增 10 个纯测试通过及 35 个 PG 跳过。没有改生产或测试文件，不代表六站回归。

同批主控重跑本文件 T6 起点的 9 文件共享纯测试组合：**138 passed / 2 既有 warnings，2.70s，退出 0**。不连接 PG，与前述 Web 组存在重叠，不能累加为不重叠测试总数。

本机默认 pytest 临时目录存在权限风险，因此分别追加唯一工作区 `--basetemp`；启动前核对两目录均不存在，未覆盖既有目录：`pytest-t6-web-31faf7b0b512443fbefededa8694dabe` 与 `pytest-t6-shared-2e3798aa1d1f4613b511b08b97a64476`，父目录为本执行记录目录。其清理状态另记；这只是单测试进程参数，不改全局 fixture 或 H5/App 配置。

`git diff --check` 退出 0，只有既有 LF→CRLF 配置警告，没有通过 Git 写回文件或修改索引。T6 独立审查尚未完成。

主控临时文件清理：Web 专用 UUID 目录共 26 个条目、无重解析点，解析后核对绝对路径及直接父目录，通过原生 PowerShell Remove-Item 清理；共享纯测试并未创建其 basetemp。仅删除可重建 pytest 临时样本，源码、报告、补丁和隔离 PG 均保留。

### T6 首轮 P1 复修后主控新鲜回归（2026-09-10）

实现者停止生产/测试编辑后，主控采用相同绝对路径 Get-ChildItem 顺序执行所有 test_web_*.py：**1377 passed /119 skipped /4 既有 warnings /42.92s，退出0**。未设置 PG DSN；新增13项真实 readiness PG 在此跳过，由实现者最终隔离PG组合实际执行，不把跳过冒充通过。共享图、Coordinator、Web assessment、App 纯边界9文件：**138 passed /2 既有 warnings /2.59s，退出0**；具体9文件仍与前轮组合一致。

两命令分别使用启动前确认不存在的唯一仓库根 basetemp：pytest-t6-fix-web-eb6e45c38be747aaac5b334335739b5b、pytest-t6-fix-shared-95e482275fa24ced81834f22b01dbc79。前者26项、无重解析点，核对精确绝对路径和直接父目录后已用原生 PowerShell 删除；后者未生成。只清理本轮可重建测试样本，不影响源码、报告或数据库。

task-6-review-fix-package.md 保存相对首轮交付的3路径净差异，均经重建及往返核验；task-6-fix-scope-audit.json 保存96源码路径范围对照，恰为3路径改变、93未变。此结果供原审查者复审，不代表 T6 已获 Approved 或后续 T7～T10、六站已完成。

### T6 第二轮 P1 复修后主控新鲜回归（2026-09-10）

实现者完成格式整理并停止生产/测试编辑后，同样的全Web绝对路径命令：**1377 passed /136 skipped /4 既有 warnings /43.23s，退出0**；PG DSN 未配置，新增真实Chrome+PG与负向组在此跳过。9文件共享纯边界：**138 passed /2 既有 warnings /2.75s，退出0**。仍不累加重叠组，不拿此次结果代替实际PG或独立审查。

全Web临时目录 pytest-t6-fix2-web-55129ddec87b41d286d8b034c1f5b659 在运行前确认不存在，结束后核实仓库直接子路径、26项且无重解析点，只清理这些可重建测试样本。共享纯组未申请basetemp，未清理别的目录。源码、报告与隔离PG保留。

第二轮 task-6-review-fix2-package.md 的2路径差异以 task-6-review-fix2-baseline.json 重建并往返校验；task-6-fix2-scope-audit.json 记录96路径范围，其中2变94未变。第一轮复审已确认导航修好但真实OBSERVE null回执仍缺观察身份，本次验证针对该剩余P1，不改写上一轮未通过结论。

### T7 入口与共享行为起点（2026-09-10）

T6最终Approved并保存task-7-baseline.json后，T7实现者读简报阶段，主控新鲜运行：`test_capture_coordinator.py`、`test_surface_driver_adapter.py`、`test_h5_runtime_composition.py`、`test_worker_web_v2_route.py`、`test_skills_loader.py`，命令为仓库.venv Python执行 `-m pytest` 以上5文件并追加 `-q -p no:cacheprovider`。结果 **282 passed /2 既有 warnings /3.94s，退出0**。本组为入口/共享边界基线，不能代替T7修改后验收，不与之前重叠组累加。主控未修改源码，未连接PG；隔离PG由T7实现者使用。

### T7 主控几何交叉检查（实施中，2026-09-10）

主控以 `.venv/Scripts/python.exe -B -` 纯内存脚本调用 `viewport_rectangles_coverage`，固定随机种子7，生成300组整数视口（宽高1～12）及矩形（坐标-8～15、宽高0～15、每组0～12个），逐像素格点枚举作为独立并集面积基准；300组全部相符，容差1e-12，退出0、0.42s。仅辅助核验重叠/离屏裁剪算法，不是pytest用例数，不代表T7整体或真实PG链通过；无浏览器、数据库或文件写入。

### T7 首次交审后主控新鲜回归（2026-09-10）

实现者全部STOP后，主控生成task-7-review-package.md与task-7-scope-audit.json：105源码路径、22变83未变，12生产/10测试，无迁移。分项差异以原task-7-baseline.json重建并往返验证，保留原始交审包，不覆盖旧基线。

主控使用与T6相同的Get-ChildItem绝对路径顺序运行全体test_web_*.py，显式清空本测试进程的PG/Chrome环境，追加唯一已核验不存在的仓库根basetemp：**4 failed /1414 passed /139 skipped /4既有warnings /40.32s，退出1**。跳过含环境受控的PG/浏览器项，另由实际PG/Chrome组验证，不计作通过。四个失败为test_web_reused_evidence_bundle.py的reader_build四场景，fake qualification未返回新增的finalization_epoch，实际SQL已返回该字段。定点 `-k reader_build --tb=short -q -p no:cacheprovider` 再现 **4 failed /5 deselected /2warnings /0.53s，退出1**；待原实现者修复并由独立审查者复验，未把广回归称为通过。

共享13文件（capture_coordinator、surface_driver_adapter、h5_runtime_composition、worker_web_v2_route、skills_loader、page_graph、page_graph_projection、page_graph_progress、web_page_type_assessment、app_surface_evidence、app_capture_state_invariants、app_graph_recapture、app_evidence_index）的对应test_*.py：**371 passed /2warnings /5.37s，退出0**。不连接PG，与广Web/实现者组有重叠，不累加。

主控临时目录g7web-c349ea81和g7shared-38c2ab94均在启动前确认不存在，结束后核对为仓库直接子路径、无重解析点；分别26/10个子项（含目录自身27/11），仅以原生PowerShell删除这些可重建测试样本。源码、报告、审查包、隔离PG和其他人的临时文件均保留。主控当前没有后台测试进程，PG交由T7独立审查者独占。

临时 PostgreSQL 容器停止后 tmpfs 数据会丢失。完成所有 Web PostgreSQL 验证前保留，仅允许清理本轮明确创建的容器，不操作其他 Alioth 容器和数据卷。

## T7 P2-4 单测试修正后主控最终回归

唯一测试函数修正并全部STOP后，105路径范围核验为1变更/104不变；生产与PG测试均保持第二轮独立验证版本。主控运行相同Get-ChildItem绝对路径顺序的83Web文件，显式清空该进程PG/Chrome变量：**1430 passed /139 skipped /4既有warnings /41.15s，exit0**。未连接PG，未把第二轮67PG改记成本次执行；原审查者第三轮进行中。

```powershell
$env:PYTHONUTF8='1'
$env:ALIOTH_TEST_DATABASE_URL=$null
$env:ALIOTH_DATABASE_URL=$null
$env:ALIOTH_TEST_CHROME_EXECUTABLE=$null
$gapTemp='D:/project/Python/alioth/g7f2w-72deff49'
if(Test-Path -LiteralPath $gapTemp){throw '本次测试目录已存在'}
$gapWebTests=@(Get-ChildItem -LiteralPath 'services/api/tests' -Filter 'test_web_*.py' -File | Sort-Object Name | ForEach-Object { $_.FullName })
& '.venv/Scripts/python.exe' -B -m pytest @gapWebTests -q -p no:cacheprovider --basetemp $gapTemp
```

本轮唯一主控临时目录 `D:/project/Python/alioth/g7f2w-72deff49` 在创建前确认不存在，结束后验证为仓库直接子目录、27项含根/0重解析点，仅删除该中性可重建测试样本并确认不存在。未删除源码/用户文件或容器；主控无运行中测试进程。

## T7 复修交审后主控新鲜回归

实现者明确全部STOP后执行两组，未使用PG/Chrome环境变量；真实PG/Chrome由实现者和原审查者另组验证。全Web **1430 passed /139 skipped /4既有warnings /40.75s，退出0**；比实现者显式Chrome组多1个浏览器环境跳过。13共享文件 **371 passed /2既有warnings /5.38s，退出0**。不累加重叠组，不代表所有PG、六站或正式Gate已验收。

```powershell
$env:PYTHONUTF8='1'
$env:ALIOTH_TEST_DATABASE_URL=$null
$env:ALIOTH_DATABASE_URL=$null
$env:ALIOTH_TEST_CHROME_EXECUTABLE=$null
$gapTemp='D:/project/Python/alioth/g7fw-fbbb06f8'
if(Test-Path -LiteralPath $gapTemp){throw '本次测试目录已存在'}
$gapWebTests=@(Get-ChildItem -LiteralPath 'services/api/tests' -Filter 'test_web_*.py' -File | Sort-Object Name | ForEach-Object { $_.FullName })
& '.venv/Scripts/python.exe' -B -m pytest @gapWebTests -q -p no:cacheprovider --basetemp $gapTemp
```

```powershell
$env:PYTHONUTF8='1'
$env:ALIOTH_TEST_DATABASE_URL=$null
$env:ALIOTH_DATABASE_URL=$null
$env:ALIOTH_TEST_CHROME_EXECUTABLE=$null
$gapTemp='D:/project/Python/alioth/g7fs-fbbb06f8'
if(Test-Path -LiteralPath $gapTemp){throw '本次测试目录已存在'}
& '.venv/Scripts/python.exe' -B -m pytest services/api/tests/test_capture_coordinator.py services/api/tests/test_surface_driver_adapter.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_worker_web_v2_route.py services/api/tests/test_skills_loader.py services/api/tests/test_page_graph.py services/api/tests/test_page_graph_projection.py services/api/tests/test_page_graph_progress.py services/api/tests/test_web_page_type_assessment.py services/api/tests/test_app_surface_evidence.py services/api/tests/test_app_capture_state_invariants.py services/api/tests/test_app_graph_recapture.py services/api/tests/test_app_evidence_index.py -q -p no:cacheprovider --basetemp $gapTemp
```

原首审之后12源码/测试路径变更（5生产7测试），105路径中93不变；task-7-review-fix-package.md、task-7-fix-scope-audit.json保存本轮净变更。每项before SHA匹配首审交付SHA、current SHA匹配STOP后核验且补丁往返成功。首审Markdown曾去掉末尾空白上下文，另存当时完整22条原diff为task-7-review-exact-diffs.json，逐条正文与首审SHA一致；没有修改旧历史或凭空补造源码。

上述两个主控临时目录分别27/11项（含根），均为仓库直接子目录、0重解析点；测试完成后再次核对相同边界并仅删除这两个中性可重建样本目录，确认均不存在。未删用户文件，未操作PG。原审查者现在独占隔离PG，主控没有后台测试进程。

## T9 Docker 短暂探测阻塞与恢复（2026-09-10）

约20:52（Asia/Shanghai），实现者在能力修订 PG GREEN 前的 Docker 探测停滞，尚未进入数据库身份校验或 pytest；仅终止自身会话75436，没有重启 Docker、测试库或共享服务。未知归属进程保持不动。

主控只读探测确认127.0.0.1端口55439、5432、6379、8000、5173均可连接。对准确容器 `alioth-web-gap-test-20260909` 执行带12秒子进程超时的 `docker inspect --format '{{.State.Running}}|{{.Config.Image}}'`，实际返回 `true|pgvector/pgvector:pg16`，退出0、无stderr，执行约0.45秒。没有读取密码或连接PG，不将TCP可连接冒充业务健康。

恢复后仍由T9实现者独占隔离PG；其原凭据白名单探测补充20秒超时，后续必须重新核实数据库、角色与Alembic head后才执行测试。本次环境检查不是能力修订GREEN结果，主控无后台测试进程。

后续实现者探测会话5634仍无输出，已仅终止自身会话并确认无其Python/Docker进程。复核原命令发现Python输出缓冲、数据库连接没有独立超时，因此实际阻塞位置未知，不能仅凭无stdout断言Docker挂起。

主控在实现者明确暂停PG后，执行一次 `python -u -B` 分阶段检查，Docker只格式化返回 `POSTGRES_PASSWORD` 白名单并在内存处理，不打印密码/DSN/异常正文；子进程超时12秒，asyncpg连接/查询/关闭分别限时5秒。实际依次输出 python_ready、asyncpg_imported、inspect_returned exit0、pg_connect_start，随后确认 `alioth_test_web_v2|alioth_web_gap|20260910_0103`，执行0.70秒退出0。连接finally关闭后立即归还实现者，没有运行pytest或写库；后续实库测试仍需独立记录。

记录更正：早期无缓冲命令没有阶段stdout，只能说明当时没有可核验的完成输出，不能据此断定“尚未进入身份校验或pytest”。后续发现三个中断测试Task残留，并以有界用例确认能力仓储重复acquire导致等待；撤回上文对早期执行阶段的确定表述。没有完成结果的中断会话不计为通过或失败测试数，具体残留归属和精确清理见T9实施报告。
