# 普通视频正式帧与跨会话报告隔离验证

日期：2026-10-16。分支：`h5-chain-reliability-and-entry-compatibility`。本轮工作未提交、未重载、未发布；不改当前业务服务、历史任务/证据、原表、风险Gate或模型预算。

## 本轮修复与证据

- 普通视频正式准入复用原生已呈现帧与双观测合同；最后观测PNG成为唯一主帧，同Capture附件与实际SHA闭包验证，旧截图/加载/遮挡/资源变化/缺附件不准入。
- 普通采样与检查点共用窗口，样本单独存附件，不扩大UI快照上限。非视频/支付展示和明确广告、隐藏、屏外媒体有反例。
- 实际落库发现`checkpoint_observation`类型未注册，迁移0138补入词汇/数据库约束；有样本拒绝降级，不删除证据。仅自有隔离库迁移。
- 实际报告发现来源Epoch与H5封存Epoch不同，使用真实Gate/Coverage锁定来源证据，仅允许报告只读复核，封存后仍拒绝新动作。
- 广泛回归的首帧缺失随后确定复现为观察器注册后同步读及媒体时间错位；有界等待首个原生回调，媒体事实同次读取。没有回调、暂停无历史帧仍为未知，不自动播放，不靠重跑转绿结案。
- 同事务同时间PNG通过真实PG查询验证主帧选择，不按UUID误取旧样本。视口主帧明确保存`full_page=false`。

实现与边界详见`../../design/2026-10-16-h5-video-publication-and-recovery-report-e2e.md`及专项回归规则。

## 最终验证分组

用例分组有重叠，禁止把以下数字相加为独立验收总数。

| 输出 | 范围 | 结果 |
| --- | --- | --- |
| `pg-final.xml` | 检查点、恢复绑定、流程消费、正式报告、支付/会话隔离、Publication真实PG回归 | 104通过，0失败，0跳过 |
| `migration-compat-final.xml` | 迁移兼容与不可破坏审计约束，独立新库运行 | 7通过，0失败，0跳过 |
| `primary-binding-and-e2e-final.xml` | 同时间最终主帧真实PG、元数据反例、最终跨会话整链 | 13通过，0失败，0跳过 |
| `native-and-primary-final.xml` | 最后修复后的禁网原生浏览器、合同、读取、存储及入口兼容 | 158通过，0失败，0跳过 |
| `broad-complete.xml` | 全部选定H5/检查点/流程/采集/报告广泛回归 | 4386通过，1失败，7跳过；未全绿 |

最后两组覆盖首帧时序修复。`pg-final.xml`之前完成，最终整链随后在`primary-binding-and-e2e-final.xml`再验证；后者不是业务任务重跑。

### 整链具体边界

第一检查点 → 关闭来源会话 → 实际Snapshot请求/租约 → RestorationCoordinator与实际恢复账本/等价评估 → 新会话动作、Capture、最新Manifest → 第二检查点 → 实际InputSet/Work/Outbox/Candidate → 通用Judge → 实际CompletionGate → 正式正文 → 封存后重新绑定。

生产仓储、可信附件读取器、状态机和报告持久化未替换。恢复执行、视频UI事实/PNG和事件运输由受控测试端口供给；不是原站浏览器恢复或Redis实网验证。原生视频采样另用网络全部中止的自有Chromium检验。覆盖快照按实际受控Capture由领域代码构造，不替代真实模型页型判断。

反例：缺恢复动作、错误同会话许可、旧Epoch、晚到失败Manifest、待复核、篡改报告恢复引用；无权威Gate不能复用封存来源，允许报告复核也不能准入新动作。正例有3个实际动作、2个检查点，原200预算/截止保持、模型准入和用量事件均为0，不能由此判断真实200次预算是否足够。最终Task质量仍为unknown，不把Gate收口当业务全通过。

### 跳过项

广泛命令不提供数据库连接，5个迁移用例和2个要求原库事务的关闭会话浏览器用例跳过。迁移用例已另在显式新隔离库验证；2个原库浏览器用例没有执行，不为跑绿连接业务库。受控跨会话整链与这些物理浏览器用例不是同一证据。

## 失败记录保留

- `first-red.xml`、`admission-red.xml`、`artifact-vocabulary-red.xml`、`source-seal-epoch-red.xml`、`non-business-video-red.xml`保留合同、类型、Epoch、非业务媒体等修复前记录。
- `first-frame-race-red.xml`：2失败、1通过，确定复现首帧回调缺失和旧媒体时间；`first-frame-race-final.xml`：34通过。保留`broad-final.xml`中首次播放采样的实际失败，不用后续成功抹掉。
- `broad-first.xml`：4374通过、8失败、7跳过。8项入口兼容假页面缺已有Canvas观察器CDP端口，修正测试夹具而不是放宽生产异常处理。
- `broad-final.xml`：4383通过、1失败、7跳过。失败为首帧竞态，后补确定性反例再修，完整验证结果另存`broad-complete.xml`。
- `broad-complete.xml`唯一失败是`test_real_browser_visual_click_binds_stable_after_frames_to_action_trace`。源DOM没有video，动作前Canvas状态已为`context_lost=true`，像素采样为透明；三张PNG SHA均为`04d6522f25e1ece5103c9169cd7845f1076f1987f046f0fe34b25d73fd0fac92`，与10月13日历史失败帧逐字节一致。匹配1.0仍被`VISUAL_TARGET_OBSTRUCTED`正确拒绝，不能靠相同破图授权点击。普通视频采样尚未执行。原始材料保存在`canvas-loss-broad-case/`；原生丢失发生的具体GPU/压力/生命周期触发未证实，保持阻断，不单独重跑失败浏览器用例转绿。
- `cross-session-*`、`formal-gate-*`包含新夹具缺字段/时序/审计删除冲突等构造错误，不一律称为产品根因；其中实际类型约束、Epoch错误另有直接复现证据。
- `pg-first.xml`包含隔离角色防线拒绝，未改防线；`pg-second.xml`的临时表缺真实库已有metadata列，修正夹具。`primary-binding-final.xml`是新测试调用隔离防线少参数，后续文件修正，不是产品失败。
- `primary-reader-final.xml`为选错不存在测试文件导致0执行，不作为通过证据；有效验证见最终分组。

## 执行与资源

- PostgreSQL16自有数据目录：`/tmp/alioth-journey-contracts-20261016-pg`，端口56436；新库`alioth_video_publication_regression_test`，旧自有测试库与所有审计图保留。
- 不可变Gate/正式报告已持久化的隔离图不再删除；普通无正式Gate的夹具仅清自身临时数据。没有清业务历史。
- 最后数据库测试后已停止自有PostgreSQL；当前业务API/Worker/前端没有操作。Chromium由各自测试夹具在finally关闭，未控制用户浏览器。
- 静态检查对本轮涉及的后端与测试文件通过，`git diff --check`通过；最终源码SHA记录见`source-sha256-final.txt`，较早`source-sha256.txt`也保留，不包含敏感配置。

广泛回归命令范围为`test_(h5|checkpoint|journey|aggregate_inspection|page_capture|capture_runtime|ui_snapshot_reader|evidence_exit_requirement_resolver|report_insight|task_finalization|evidence_manifest_writer)*.py`，排除文件名含postgres/soak的项。数据库分组通过显式隔离连接运行，不读取业务数据库。

## 未验证与保留阻断

1. 原站支付/真实视频覆盖、真实模型调用与CNY、Redis实网、180分钟容量未验证；本轮不承诺所有业务无回退，不具备发布结论。
2. 无后台只读契约的充值继续阻断；私有高清没有明确资源配对继续阻断，不猜接口白名单或URL关系。
3. 私有Canvas/WebGL、iframe/shadow播放器、多个正文媒体、首次暂停且未观察原生呈现仍未知；不借稳定几何放行。
4. 顺序截图/DOM不是原子观察；全页视频和普通滚动/定时序列无本帧合同不能补算正式覆盖。历史证据不回写追认。
5. 新迁移0138尚未应用业务库，代码未重载/发布。隔离验证通过不代表当前服务已实现新行为。
6. 广泛回归再次观察到动作前原生Canvas丢失，本轮没有实际GPU/process/事件发生时刻记录，不能补造触发归因，整套回归未全绿，仍不具备发布结论。
