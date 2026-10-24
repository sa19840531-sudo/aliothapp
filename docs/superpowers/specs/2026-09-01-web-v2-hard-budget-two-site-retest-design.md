# Web V2 硬预算、阻断闭环与六站复测设计

> 日期：2026-09-01  
> 状态：已经用户确认  
> 适用范围：`WEB_V2` / `desktop_fhd_1920x1080_v1`  
> 替代范围：本文仅替代旧设计中与「固定 `z-index > 500`」、成功终态和六站验收相冲突的部分

## 1. 目标

1. Web 与 H5/App 探索继续保持路由、Runtime、页面目录、队列和采集预算隔离。
2. 每个 Web 站点最多采集 1 个首页和 10 个 Catalog 同源菜单。
3. 列表菜单最多并且必须成功采集 1 个可信详情；菜单本身已是详情页时不再执行详情动作。
4. 处理 `heiliao.com` 与 `www.91jav1.com` 首页普通阻断，不绕过人机验证。
5. 每个首页、菜单、详情和阻断动作都持久化截图、DOM、URL 与动作证据。
6. 任何缺少详情、页面未分类、选择不可信、类型不匹配或证据不完整的结果都不得标记成功。
7. 修复后依次复测六站，输出过程节点、页面类型、证据索引和 Markdown/HTML 采集报告。

## 2. 非目标

- 不修改 H5/App 的采集流程、页面类型、弹窗策略、预算或 Worker 路由。
- 不处理 CAPTCHA、滑块、「我不是机器人」、Cloudflare 挑战或其他人机验证。
- 不把第三方内容、口令、短期令牌、下载票据或完整证据下载 URL 写入报告。
- 不在本次修复工作台 422、H5 重跑/复制、全量质检或生产 Worker 部署。
- 不通过站点专用业务选择器绕过通用候选安全校验。

## 3. 已核实的现状

- `WebMenuExplorer` 已在单次进程内只执行前 10 个同源白名单菜单。
- `WebListDetailNavigator.capture_one()` 单次调用最多发出一次详情 Navigate。
- 上述两个限制没有持久化执行预算，不能防止 Worker 重试或恢复后重复消耗。
- `WebExplorationAssessmentAgent` 在模型判定年龄门禁存在但没有安全候选时，会把结果改写为「无门禁」。
- 普通阻断判定也存在「模型说存在、没有结构候选则改写为无阻断」的分支。
- `_web_exploration_is_partial()` 未覆盖 `no_detail_candidate`、`page_type_unclassified`、`detail_selection_untrusted` 和 `detail_page_type_mismatch`。
- 首页在 `prepare_home()` 中失败时，完整 Catalog 账本尚未建立，会形成空的 Web 摘要。
- 最近六站实测为首页 `4/6`、同源执行 `33/54`；`heiliao.com` 与 `www.91jav1.com` 均终止于 `WEB_HOME_POPUP_BLOCKED`。

## 4. Web-only 边界

### 4.1 允许修改

- `services/api/app/application/web_*`
- `services/api/app/agents/explorer/web_assessment_agent.py`
- `services/api/app/domain/exploration/web_exploration.py`
- `services/api/app/infrastructure/browser/page_capture.py` 中只由 Web Desktop Policy 触发的 Snapshot 候选生成分支
- `services/api/app/infrastructure/db/web_*`
- Web 专属数据库迁移表
- `services/api/tests/test_web_*`
- `scripts/web_v2_six_site_shadow.py`
- `docs`

### 4.2 隔离门禁

- Web 仍只通过 `WEB_V2` 路由。
- Runtime 必须保持 `include_h5_exploration=False`、`logical_surface_enabled=False`、`web_desktop_policy_enabled=True`。
- 新预算仓储仅能从 `PostgresWebRuntimeProvider` 注入。
- 新表、约束和查询必须要求 `surface_kind='web'` 且 Session owner 为 `web-v2-workflow`。
- H5/App 边界回归必须在代码审查前通过。

## 5. 持久化执行预算

### 5.1 两表语义

新建两张 Web-only 表：

1. `qc_web_exploration_budget`：一行对应同一 Run 与站点的冻结预算。
2. `qc_web_menu_ledger_entry`：每行对应一个 Catalog 菜单的唯一执行权和证据账本。

`qc_web_exploration_budget` 必要字段：

```text
agent_run_id uuid
site_id varchar(64)
task_id uuid not null
session_id varchar(160) not null
target_origin text not null
catalog_version varchar(128) not null
catalog_sha256 varchar(64) not null
menu_limit smallint not null default 10
state varchar(32) not null
row_version bigint not null default 0
created_at/updated_at/sealed_at timestamptz
PRIMARY KEY (agent_run_id, site_id)
CHECK (menu_limit = 10)
```

`qc_web_menu_ledger_entry` 必要字段：

```text
agent_run_id uuid
site_id varchar(64)
menu_entry_id varchar(160)
catalog_position integer not null
decision varchar(64) not null
menu_path jsonb not null
canonical_url text not null
execution_slot smallint
status varchar(64) not null
menu_action_count smallint not null default 0
menu_action_execution_id varchar(160)
menu_probe_capture_id uuid
menu_ready_capture_id uuid
menu_page_type varchar(64)
detail_action_count smallint not null default 0
detail_action_execution_id varchar(160)
detail_probe_capture_id uuid
detail_ready_capture_id uuid
detail_page_type varchar(64)
detail_selection jsonb not null default '{}'
diagnostic jsonb not null default '{}'
row_version bigint not null default 0
created_at/updated_at timestamptz
PRIMARY KEY (agent_run_id, site_id, menu_entry_id)
FOREIGN KEY (agent_run_id, site_id)
  REFERENCES qc_web_exploration_budget(agent_run_id, site_id)
```

安全约束：

```text
UNIQUE (agent_run_id, site_id, execution_slot) WHERE execution_slot IS NOT NULL
UNIQUE (menu_action_execution_id) WHERE menu_action_execution_id IS NOT NULL
UNIQUE (detail_action_execution_id) WHERE detail_action_execution_id IS NOT NULL
CHECK (execution_slot BETWEEN 1 AND 10 OR execution_slot IS NULL)
CHECK (menu_action_count IN (0, 1))
CHECK (detail_action_count IN (0, 1))
CHECK ((detail_action_count = 0 AND detail_action_execution_id IS NULL)
    OR (detail_action_count = 1 AND detail_action_execution_id IS NOT NULL))
```

### 5.2 预算协议

```python
class WebMenuExecutionLedger(Protocol):
    async def initialize_catalog(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        site: WebSiteMenuDefinition,
    ) -> None: ...

    async def reserve_menu(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        site_id: str,
        menu_entry_id: str,
    ) -> int | None: ...

    async def claim_detail(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        site_id: str,
        menu_entry_id: str,
        action_execution_id: str,
    ) -> bool: ...

    async def record_terminal(self, record: WebMenuLedgerTerminalRecord) -> None: ...
```

规则：

1. Catalog 解析后、打开首页之前就建立全量账本。
2. 首页、跨域和超限条目的 `execution_slot` 为 `NULL`。
3. 只有 Catalog 顺序中前 10 个同源可执行菜单可预留 `1..10` 槽位。
4. 同一 Run 重试时返回原槽位，不新增槽位；已终态条目不再执行。
5. `claim_detail()` 在详情动作发出前执行；已经有详情动作时必须失败关闭。
6. 跨域、超限和首页阻断未执行条目不得关联菜单 Capture 或 ActionExecution。

## 6. 页面与详情预算

| 页面语义 | 菜单采集 | 详情动作 | 成功终态 |
|---|---:|---:|---|
| `web_video_list` | 1 | 恰好 1 | `detail_captured` + `web_video_detail` |
| `web_content_list` | 1 | 恰好 1 | `detail_captured` + `web_content_detail` |
| `web_video_detail` | 1 | 0 | `detail_captured` |
| `web_content_detail` | 1 | 0 | `detail_captured` |
| 未分类/不可信 | 1 | 0 或 1 | `needs_review` |

`detail_action_count` 必须从持久化 ActionExecution 关联派生，不只信任返回值。

## 7. 年龄门禁与普通阻断

### 7.1 年龄门禁

- 有效成年人自我声明是点击的前置条件。
- 模型返回 `gate_present=true` 且没有密封的 `web_age_gate_confirm` 候选时，返回待复核，禁止改写为无门禁。
- 人机验证一律返回 `human_verification_blocked`，不签发动作。
- 点击后必须重新读取截图、DOM 和 URL，证明门禁消失且仍同源。

### 7.2 普通阻断

禁止使用固定 `z-index > 500` 作为候选门槛。判定必须结合：

- 1920×1080 当前截图中是否遮挡主内容或主交互区。
- DOM 中 dialog/modal/overlay/popup/mask 语义、显式打开状态和视口重叠。
- 当前可见性、`pointer-events`、点击命中与主体滚动锁。
- 关闭候选是否位于该阻断容器，或是安全的委托点击祖先。
- 候选是否具有 close/dismiss/cancel/skip 语义，且不是登录、支付、下载、广告跳转或内容链接。

如果模型判定存在阻断但没有安全关闭候选，返回 `popup_blocked`，不得改写为无阻断。

### 7.3 两站专项

- `91jav1.com`：支持 `#popup-window` 内无文本图片关闭与 `.on-popup-close` 委托包装节点，但仍必须通过候选闭集、命中测试和消失复验。
- `heiliao.com`：根据已持久化首页 Capture 提取通用阻断结构事实；如实况是非阻断悬浮广告，不予关闭也不影响首页就绪。

## 8. 证据完整性

每个执行的首页、菜单、详情和阻断动作必须具备：

```text
capture_id
final_url
screenshot artifact (PNG, 1920×1080)
dom artifact
ui_snapshot artifact
visible_text artifact
action_execution_id (发生动作时)
action_trace artifact (发生动作时)
source_capture_id / after_capture_id
page_type / confidence / reason
```

规则：

- Web 捕获重试或体积降级不得删除 DOM。
- 任一必需证据缺失时，当前菜单/详情进入 `needs_review`。
- 报告只输出 ID、摘要和统计，不嵌入敏感 DOM、短期票据或完整下载 URL。

## 9. 成功判定

采用失败状态闭集；以下任一计数大于零时都必须为 `NEEDS_REVIEW`：

```text
popup_blocked
human_verification_blocked
unsafe_navigation
navigation_failed
allowlist_drifted
no_detail_candidate
page_type_unclassified
detail_selection_untrusted
detail_page_type_mismatch
evidence_incomplete
budget_conflict
```

必须同时满足：

- `frontier_remaining == 0`
- 所有执行菜单终态均为 `detail_captured`
- 列表菜单的持久化详情动作数为 1
- 直接详情菜单的持久化详情动作数为 0
- 跨域和超限条目没有 Capture/Action 关联
- 证据完整性检查通过

## 10. 六站复测合同

固定顺序：

1. `https://heiliao.com/`
2. `https://hl365.com/`
3. `https://www.91jav1.com/`
4. `http://91porna.com/`
5. `http://huangguoai.com/`
6. `https://avhub360.com/`

预期总量：

| 指标 | 期望 |
|---|---:|
| Catalog 条目 | 166 |
| 首页就绪 | 6 |
| 同源菜单执行 | 54 |
| 超限跳过 | 103 |
| 跨域阻断 | 3 |
| Frontier | 0 |

分站执行菜单数是 `10/10/6/10/8/10`。详情逻辑数等于执行菜单数：列表菜单是新详情 Capture，直接详情菜单以菜单 Ready Capture 作为详情证据。

每站报告顺序：

```text
Catalog 校验
-> 任务/Run 回执
-> 首页 entry probe
-> 年龄门禁结果
-> 普通阻断关闭链
-> 首页 ready + home 类型
-> 菜单 source/navigate/probe/ready/page type
-> 详情 candidates/selection/navigate/probe/ready/page type
-> 菜单终态与证据索引
-> 任务终态与聚合统计
```

实测令牌只能通过正式登录接口在当前进程内获得，不得输出、写入文件、命令行或报告。

## 11. 验收与安全停止

- 代码验收：Web 专项、数据库仓储、迁移、H5/App 边界回归、Ruff 全部通过。
- 审查验收：每个实施任务的 Critical/Important 问题全部处理，再进行整分支审查。
- 实测验收：六个任务都获得 final report，且满足第 10 节计数和第 9 节成功门禁。
- 如出现人机验证，不执行点击；当站以 `human_verification_blocked` 停止并如实进入报告。
- 如外站临时不可用，只有在已留存截图、DOM、URL 和动作证据后才允许有界重跑失败站点，不扩大菜单或详情预算。
