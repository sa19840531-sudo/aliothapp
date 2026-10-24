# 证据展示层门禁（可见层 + 自然/诊断隔离）交付审查

日期：2026-09-07。对应排期「统一证据来源和页面层级校验」与「区分自然操作证据和诊断证据」（立川），
设计依据：专项七《可信证据留存》§5.2/§5.5、实现级设计 §8.6（PresentationStackAssessment）/§18.7。分支 `feat/evidence-presentation-gate`，基线 `dev_2.0@8e0643a1`。

## 1. 交付边界

- 新增展示层评估合同 `presentation-stack.v1`（`domain/evidence/presentation.py`）：最上层是谁（page / overlay / system_dialog / external / unknown）、底层页面可见度（visible / partially_occluded / fully_occluded / unknown）、阻断范围、覆盖面积、命中测试证明力、自然/诊断标记与诊断操作记录。
- 推导只用两端已有事实，不改采集协议（`application/evidence_presentation.py`）：
  - H5：UI Snapshot 的矩形与命中测试（复用 `assess_h5_ui_obstruction`，新增 `max_blocking_coverage`）+ 识别出的页面状态 + action_log 中已有的诊断痕迹（`fallback=verified_high_z_obstruction_hide`、`resolved_by=*_direct_dispatch_*`、`fallback_dispatch_attempts`）。
  - App：视觉观察表的 `page_state / overlay_signature / surface_facts`（含 `topmost_ad_popup.normalized_bounds` 面积）。
- 写入口：H5 持久化服务在落库时把评估写进 `qc_page_capture.metadata.presentation`（触发器同步到 `qc_capture`）；仓储在同一事务里继承同一 Surface Session 内更早采集的诊断操作（恢复样式不等于恢复自然状态）。App 不改采集行，读侧由视觉观察推导。
- 消费口三处统一：
  - 判定包 `facts.presentation`（同页共享，不破坏提示缓存前缀）；缺持久化评估时从原始快照 / 视觉观察兜底推导，并用识别状态校正。
  - 判定预检 `_presentation_preflight`：语义/混合、无执行器的确定性规则、带执行器的确定性规则三条路都过；拒绝即 `evidence_insufficient`，`judge_output` 带 `presentation_rejection` 与 `missing_evidence`，`evidence_trusted=false`。
  - 聚合封存前 `_select_rule_evidence` 剔除被拒成员；全部被拒记 `AGGREGATE_PRESENTATION_EVIDENCE_REJECTED`（needs_review）；选择策略哈希升到 `aggregate-selection-policy.v2`。
- 规则要求的证据层 `rule_evidence_layer_target`：规则配置 `evidence_layer_target` 显式声明 > 广告类规则任意层 > 检查点提到弹窗/蒙层/遮罩任意层 > 默认要求底层页面可见。诊断材料只有 `accepts_diagnostic_evidence=true` 的规则才接受。
- 判定指令追加一句：底层被遮挡的内容不是当前页面可见证据；诊断材料不是正常用户操作证据。
- 清单承载来源事实：H5 持久化服务先叠加同 Surface Session 的诊断继承，再把同一份展示层评估写进 pending 清单每份附件的 `metadata.presentation`（进入 `manifest_hash`，裁决追加版本时原样保留），报告结论可从清单逆向追到"当时最上层是谁、是否经过诊断操作"。清单质量仍只裁齐全性（E02/E14）：一次采集同时服务要浮层的广告规则与要底页的文案规则，逐规则的层级接受留在封存前与判定前。
- 收口兜底事实：`CompletionGateFacts.natural_evidence_presentation_violation_count` 统计当前阶段 pass/fail/warning 结论里消费了被遮挡底页或诊断材料的条数（同一套 Python 裁决，H5 读采集行、App 读视觉观察），大于 0 时阻断 `natural_evidence_presentation_violation`；0 时不进哈希材料，历史快照身份不变。迁移 `20260907_0096` 把该原因纳入数据库公式，空库升级、降级到 0095 再升级均通过。

## 2. 主审修正（实测驱动）

1. 识别状态 `popup` 不能单独压过 DOM 结构。性巴克首页第 6/9 帧结构确认无阻断元素，识别却报 popup（实为左下角悬浮推广条），第一版按识别拒了 10 条规则。改为：普通 DOM 页面结构正面扫过且无阻断时以结构为准，只把分歧记进 `uncertainty=page_state_reports_overlay`；结构不可信（Flutter 语义树被激活、无快照，`hit_test_status=degraded/unavailable`）时识别状态才覆盖。系统对话框状态不在 DOM 里，不走结构优先。
2. 带执行器的确定性规则（如技术错误直出）原先绕过判定包，会拿被遮挡的 DOM 判"直出"；补上同一预检，只在配置了判定包服务时生效，旧接线不变。
3. 拒绝必须有正向依据：没有评估的历史采集、最上层是页面但可见度未知的采集，一律不拒。

## 3. 需求与证据

| 要求 | 证据 |
| --- | --- |
| 被弹窗遮挡的底层内容不能作为当前可见页面证据 | `test_evidence_presentation.py`（门禁裁决、H5/App 推导 26 例）、`test_rule_judge_presentation_preflight.py`（三条判定路径、层级声明覆盖、批量）、`test_aggregate_presentation_selection.py`（封存前剔除与处置） |
| 诊断操作材料带标记并沿会话传播 | `test_page_capture_presentation.py`（持久化服务推导、仓储落库、会话继承加载器 SQL 合同、清单附件盖章与哈希） |
| 收口不放行消费了违规材料的自然结论 | `test_task_completion_gate.py`（事实与哈希材料）、`test_task_finalization_persistence.py`（计数 SQL 合同、六类样本计数、阻断快照原因、迁移 0096 文本） |
| 所有写入路径同语义 | H5 主采集与 Web Agent 采集共用 `PageCapturePersistenceService.persist`；清单驱动观察源透传 `presentation` |
| 实测 | 三产品 H5 真跑（性巴克、妈妈不可以、十八汇）：弹窗帧全部判为 overlay/fully_occluded、覆盖 1.0；关闭弹窗后的页面判为 page/visible；页面级规则在遮挡帧改判「证据不足（底层被遮挡）」，广告类规则照常判定 |

回归：全量后端套件 8473 通过；13 失败 + 37 报错与干净 `dev_2.0` 工作树逐项一致（Flutter 语义遮挡 6、触屏默认值 1、CLI 未安装 38、迁移头文档、站点菜单目录、启动跳过合同、Web 默认范围、用量审计守卫），本分支零新增。

## 4. 未覆盖风险与后续

- 输入集成员本身不加层级字段：成员协议 `from_dict` 严格且改字段会让存量集合不可读；成员引用的清单已带 `presentation`，追溯链闭合。App 清单未在写入时盖章（视觉观察在采集落库之后产生），读侧统一由视觉观察推导。
- 规则的目标层目前由类别与文案启发式推断，专项四 RuleScopePlan 落地后应改为显式声明；`alioth_41`（二维码在弹窗模块）已由文案启发式放行。
- 局部遮挡按整页 50% 面积阈值区分，没有按规则目标区域判断；小于半屏的模态框记 partially_occluded 不拒绝，由判定器结合 `presentation` 事实自行权衡。
- 实测发现 dev_2.0 基线问题：H5 首个导航意图的用途文案含「提交」，被命令风控词表当作提交动作、又无可信目标语义，所有 H5 任务第一步即 `COMMAND_TARGET_SEMANTICS_UNTRUSTED`。本分支只改了那句文案作止血，风控对导航意图的处理应由观星定。
- 页面级规则在弹窗墙产品上的「真判定」数会明显下降（性巴克一帧 14 通过 / 31 拒），这是把误判暴露出来，不是覆盖能力倒退；报表口径需同步说明。

## 5. 修正后复验（性巴克第三次真跑，任务 8b791f73）

- 第 4 帧（结构确认 van-overlay + van-dialog 整屏遮挡）：页面级规则仍拒 11 条 `BASE_PAGE_OCCLUDED`，广告类 3 条照常通过。
- 第 6/9 帧（结构确认无阻断、识别报 popup，实为左下角悬浮推广条）：第一版误拒的 10 条规则已全部放行进入语义判定；本轮多条转人工是「语义规则判定超过独立墙钟预算」的模型超时熔断，与门禁无关。
- 十八汇第一条探索 run 在第 10 步被风控拒绝（点击「个人中心会员支付入口：立即开通」，无可信目标语义、文案含「支付」），整条 run 以 error 结束而不是跳过该候选；平台随后由质检排空 run 收尾。这是命令风控与 H5 候选执行的衔接问题，不在本分支范围。
- 十八汇收口阶段「Completion Gate 重试失败」：探索 run 失败后任务已进 review，聚合封存持久化只允许任务状态 running，于是每次重试都报「任务阶段、Finalization Epoch、Agent Run 或规则集快照不匹配」。早上清库前的 57 条旧任务是同一症状，属基线收口恢复问题。

