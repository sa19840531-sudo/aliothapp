# App 视觉复用与覆盖写入约束：独立提交审查

日期：2026-09-07。提交基线：`bf08d4fa`。本批是九项探索优化中的基础修正，不是九项整体完成证明。

## 已修复的问题

1. 相同图片复用复制了旧 Capture 的业务身份绑定。现在读取目标 Capture 的真实截图文件、摘要、尺寸、包名和 Activity；上下文一致才复用，并重新生成目标绑定、局部状态和候选来源。
2. 真正的归属、文件摘要或尺寸错误可能退回旧理解路径。现在这些错误直接拒绝，不调用视觉 Provider、不保存观察；合法的上下文差异则重新理解。
3. 视觉绑定原来在保存之后校验。现在写入前先校验，读回再核对，错误观察不能先进入仓储。
4. `_next_coverage_metadata` 不使用调用方的动作结果与到达证明，失败或未证明到达的后态也会增加正式覆盖。现在动作后观察必须同时具备完成动作、执行成功和到达证明，才能增加页面类型、业务页面、业务证据和里程碑；初始采集和合法被动观察保留。
5. 生命周期页型与 `app_ready` 里程碑同样执行覆盖输入约束；外部应用的观察不能声明本 App 就绪。失败画面和诊断信息继续留存。

## 独立提交边界

- 仅包含视觉理解上下文、视觉复用、覆盖累计约束及对应测试。
- `app_screenshot_exploration.py` 只暂存上述局部修改，不包含未完成的调度接线。
- 包含视觉身份构建器对非 Mapping 可见层输入的保守拒绝及三个负例。
- 不包含 CLI、ADB/Xingyu 执行改造、完整补采、前端页面和数据库迁移。

## 验证

暂存区导出到 `/tmp/alioth-app-evidence-index-QwD4Bi`，实际导入路径已核对为该独立目录中的生产模块，没有读取工作区未提交的 D/CLI 模块。

```bash
PYTHONPATH=/tmp/alioth-app-evidence-index-QwD4Bi \
/Users/funshow/Desktop/0_foya/alioth/.venv/bin/python -m pytest -q --tb=short --disable-warnings \
services/api/tests/test_app_coverage_arrival.py \
services/api/tests/test_app_visual_identity_reuse.py \
services/api/tests/test_app_visual_page_identity.py \
services/api/tests/test_app_screenshot_understanding.py \
services/api/tests/test_app_screenshot_exploration.py \
services/api/tests/test_app_screenshot_exploration_cycle.py \
services/api/tests/test_app_exploration_terminal.py \
services/api/tests/test_llm_app_vision.py \
services/api/tests/test_app_visual_observation_repository.py
```

最终结果：**331 项通过，1.76 秒，退出码 0**。独立导出中的七个代码/测试文件 Ruff 与暂存差异检查均通过。测试包含真实截图字节校验，但不是实际模型或设备端到端测试。

| 要求 | 精确测试证据 |
| --- | --- |
| 操作前后观察必须属于各自 Capture | `test_same_frame_identity_reuse_rebuilds_before_and_after_binding` |
| 错误材料不能先保存 | `test_visual_binding_is_checked_before_observation_save`、`test_workflow_does_not_understand_or_save_after_real_context_rejection` |
| 只有确认到达业务页面的操作才能增加页面覆盖 | `test_coverage_metadata_and_terminal_require_trusted_arrival` 同时检查累计结果与实际 TerminalEvaluator；包括失败、未知、缺证明、可信到达和被动观察 |
| 外跳不计业务覆盖 | `test_external_passive_observation_does_not_claim_app_ready_or_business_coverage` |

## 尚未关闭的主链问题

- 本批修正的是覆盖输入消费条件，不宣称所有调用方的 `arrival_proven` 已可靠。只读审查发现 `_candidate_arrival_proven` 对无预期页型点击和非点击动作存在直接返回 True 的分支；`app_exploration_turn._frontier_arrival_outcome` 还有第二套判断。这两处需要共享基于真实前后态的证明，不能以本批测试替代。
- 后续需分别证明点击效果、返回到原业务页、滚动状态变化、弹窗被替换和被动观察；页面类型对齐不能代替业务对象或页面身份验证。
- A2 正式执行上下文、CLI B/C、D 调度和实际 Android/官方服务端到端仍在实施验收；九项目标保持未完成。

无新增数据库迁移，无需前端菜单或路由同步。本批仅本地提交，不推送。
