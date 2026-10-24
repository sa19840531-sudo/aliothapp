# App/Web 失败审计证据

原始 XML 与短流程 XML 一并逐字节保存在
`docs/validation/2026-10-16-shortflow-app-web-raw.tar.gz`；对应清单为同名 `.sha256` 文件。
仓库提交归档而非改写原始 XML，所有展开原件仍保留在本地。归档内保持原相对路径。

详见 `docs/design/2026-10-16-app-web-failure-audit.md` 与 `failure-classification.json`。
所有历史失败记录保留；最终引用 `expanded-verified-final.xml` 的 1277 通过及
`fixture-teardown-counterexamples.xml` 的 155 通过，不合并累计。

中间记录说明：
- `baseline.xml`：原始 27 项失败。
- `current-contract-red.xml`：修改生产代码前的真实预算保护缺口。
- `fixture-isolation.xml`：旧夹具隔离后，保留原断言的 22 项通过；真实缺陷仍失败。
- `coverage-version-red.xml`：文件名带 red，但测试启动与代码修改重叠，实际 22 通过；
  不把它当修复前失败证明。另以独立进程加载不可变 HEAD 快照源码，保存
  `committed-coverage-source-red.xml` 的 11 项真实失败，期间不改工作区或服务。
- `popup-disappeared-counterexample.xml`：第一版过宽上限检查挡住无弹层动作。
- `expanded-final.xml` 与 `attempt-contract-final.xml`：测试夹具还携带矛盾弹层事实或未完成课纲；
  后续拆成矛盾事实继续观察的反例，以及明确完成课纲且无弹层的补证正例，原断言保留。
- `focused-green.xml`、`attempt-contract-verified.xml`：阶段范围验证，不代替最后扩大回归。

只运行本地隔离测试；无模型请求、原站任务重跑、数据库服务启动、业务数据库写入或发布。
`sources.sha256` 只证明本轮源码文件，不证明当前 API/Worker 已加载。
