# 集成候选验证补充

候选提交：`98b5d5b2dab4f5e7decda5997ae574e28b804265`；草稿 MR !119，目标 dev_2.0。
需求分支 `9b696de3` 和目标基线 `1af817bf` 均是候选提交的真实祖先，不以补丁替代两边历史。

- `expanded-protections-final.xml`：1486 通过、无跳过，验证新增与旧 App/H5/Web 合同、规则清单、最终 Gate 及模式隔离。与 399 项专项重叠，不累计。
- `expanded-protections.xml`：首轮扩大命令误填不存在的测试文件名，退出 4、未执行；原记录保留，不能算测试失败或通过。后续改为仓库实际存在的测试模块。
- `migration-duplicate-confirmed.xml`：3 个正常/编码反例通过，1 个仓库唯一性守卫仍失败，列出 0133/0134 两对重复编号。
- PostgreSQL 隔离升级在执行 DDL 前退出 255；独立库 public 下 qc_ 表为 0。仅停止本轮新建的端口 56516 测试服务器，目录与日志保留；以前的数据库、服务和证据未动。
- 初版流水线 #1485 对应 `a34ecf8e`，后续候选推送后由既有 interruptible 配置自动取消；未手动取消、重试任何流水线或业务任务，不把它当绿色结果。
- 最新 CI #1486 对应 `98b5d5b2`。backend-lint 与 production-compose-lint 通过。
- backend-test #10137：5021 通过、6 失败、14204 跳过；不能把跳过项算通过。
  失败为通用视觉配对、可信到达收口两个参数、动态探索终态原因、能力清单与 GPU 用例。
- h5-postgres-test #10138：重复 0133/0134 的警告之后升级退出 255，实际未执行数据库回归。
  与全新本地隔离库复现一致，不只是一条 Alembic 警告。
- `ci-candidate-failures.xml`：按 CI 六项失败 ID 在本候选隔离复现，5 失败、1 通过。
  GPU 用例本地通过而 CI 缺 `ps`，是运行依赖差异，不能重测一次就称 CI 已修复。
  原件归档 `ci-reproduction-raw.tar.gz`，保留失败断言。
- 视觉配对用例只填两张截图 ID，没有封面/正文同内容身份，触发当前 cover-content 合同拒绝。
  能力清单少列实际已注册的 h5-payment-workflow / h5-player-workflow；五个对应测试文件
  相对需求分支没有变化，不能都称为本次文本冲突修复引入的失败。
- App 到达用例 goal 的分母只有 home/content_detail，而生成的流程合同另含 video_detail、
  payment、post_publish、settings。此处实际触发 page_types_incomplete，须统一任务目标与
  流程分母的合同后验证，不能只改预期或删掉支付/视频义务以转绿。
- CI #1486 于北京时间 2026-10-16 21:25:20 结束，终态 Failed：4 个作业通过、3 个失败。
  通过：backend-lint、production-compose-lint、workstation-build、workstation-test。
  失败：backend-test、h5-postgres-test、frontend-e2e。没有部署作业，也未人工重试。
- frontend-e2e #10141：20 通过、1 失败。`review-progress.spec.ts:40` 的
  `.progress-block .progress-footnote` 同时命中规则数量说明与新增的
  `h5-progress-explanation`，触发 strict mode violation。三次既有配置自动尝试都同因失败，
  不是业务进度一定未达 100% 的证明；不得删掉待复核说明或改规则缺口数量来转绿。
  GitLab 已保留截图、视频、trace 与 junit 作业产物；本轮没有清理这些证据。

流水线：<http://192.46.230.40:18929/tyzx/alioth/-/pipelines/1486>。
草稿 MR：<http://192.46.230.40:18929/tyzx/alioth/-/merge_requests/119>。

远端 dev_2.0 仍为 `1af817bfd3cbb450bb65388a049e8e3858f1f159`，dev 仍为
`020e6abdc92362147b077e6bfbdae5c1359e2a25`。保持草稿，不强制合并、不发布。
待确认已经应用哪一条迁移链，制定兼容方案后才能继续合并；不改已发布迁移编号或 stamp。

本补充记录及本轮新结果原件留在隔离工作区，不把未提交补充文档称为已经包含在 CI 提交中。
