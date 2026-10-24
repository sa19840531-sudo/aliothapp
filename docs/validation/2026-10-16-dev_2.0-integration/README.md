# 需求分支与 dev_2.0 集成候选

- 目标基线：`1af817bfd3cbb450bb65388a049e8e3858f1f159`。
- 需求分支：`9b696de35e7bde6794b25708d0f29d445d8e11b7`。
- 隔离工作区：`/Users/tianshu/.codex/worktrees/h5-current-dev2-integration/alioth`。
- 三处文本冲突已按保护逻辑并集解决：完整平台规则清单冻结后仅 H5 冻结短流程；
  App 规则清单 Gate 与 H5 流程缺口 Gate 同时保留；清障沿 dev_2.0 的显式课纲身份核对且遵守八轮上限。
- 动态模式不得恢复产品脚本点位。旧测试显式绑定冻结课纲模式；LLM 降级测试补充真实完整课纲进度与目标。
  原业务断言保留，另验证动态模式只用真实 Frontier 优先级、不能借用固定点。

## 当前阻断

两边存在同编号、不同正文的 `20261014_0133` 与 `20261014_0134`：

- H5：`20261014_0133_h5_journey_guard.py`、`20261014_0134_h5_stop_proof.py`。
- App：`20261014_0133_rule_remark_copy.py`、`20261014_0134_measurable_feedback_copy.py`。

`alembic heads` 实际报告重复编号。全新 PostgreSQL 16 隔离库的 `upgrade head`
退出码 255，报告 Multiple head revisions；本库独立端口 56516，库名
`alioth_dev2_merge_test`，没有连接业务库或以前的验收测试库。
不使用 `stamp`、不跳迁移、不直接选一边、也不改历史编号。已向用户询问是否有环境应用
任一迁移链；尚未得到确认，不猜测已有库的迁移归属。

## 隔离回归

- `focused-merge.xml`：首次合并结果 335 通过、29 失败，主要为动态/课纲合同混用。
- `focused-contract-resolution.xml`：397 通过、1 失败，旧降级测试缺完整课纲。
- `focused-protection-final.xml`：399 通过，无跳过，包含 App 两入口模式一致性与 H5/Web 合同。
- `migration-identity-red.xml`：守卫初稿遇到历史 UTF-8 BOM 解析失败，两个反例通过；
  不能把这个失败称为编号冲突证明。原记录保留，随后新增 BOM 反例并按 Python 源加载语义解析。
- `migration-bom-counterexample.xml`：2 通过、2 失败，定位守卫自身的编码兼容问题。
- `migration-duplicate-confirmed.xml`：BOM 反例与两个正常反例共 3 项通过，唯一性守卫
  1 项仍失败，明确列出双方两对同编号迁移；不能算迁移通过。
- 完整后端收集成功：19229 项，无导入/收集错误；收集不是执行通过。
- 以上 XML 保持原始内容归档，原件留在本轮 `/tmp/alioth-dev2-integration-20261016`。
- 全后端 `services/scripts` Ruff 通过；未把需求分支以前的 PG/浏览器结果冒充本候选结果。

草稿 MR !119 已创建，目标为 dev_2.0，CI 结果待核对。迁移阻断解除之前不得强制合入目标。
本轮不合并 dev、不部署、不改当前 API/Worker/前端或历史任务证据；原需求分支保持原提交。
未知规则、无只读契约的充值、原站支付/真实视频覆盖、Canvas 历史根因及长期并发继续保留阻断。
