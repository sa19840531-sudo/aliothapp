# Playwright CLI 运行与验证说明

日期：2026-09-06。范围：九项探索改造中的 CLI 观察与普通操作。默认关闭，不能用本文的本地样本结果代替业务任务或 App 设备验收。

## 1. 安装与模式

运行环境锁定 `@playwright/cli@0.1.19`，依赖锁定 `playwright/playwright-core@1.63.0-alpha-2026-08-31`。传递依赖要求 Node.js 20 及以上。

在仓库根目录执行：

```bash
(cd infra/playwright-cli && npm ci --ignore-scripts --no-audit --no-fund)
node infra/playwright-cli/patch-cdp-context.mjs
node infra/playwright-cli/patch-cdp-context.mjs --check
node --test infra/playwright-cli/patch-cdp-context.test.mjs
```

两份 API Dockerfile 都执行同样的锁版安装和补丁步骤。镜像内脚本为 `/opt/alioth-cli/node_modules/@playwright/cli/playwright-cli.js`，不能用本地相对路径覆盖镜像配置。

| 配置 | 行为 |
| --- | --- |
| `ALIOTH_H5_CLI_MODE=off` | 默认值，使用已有浏览器执行器 |
| `ALIOTH_H5_CLI_MODE=observe` | 旧执行器操作；CLI 读取同一页并保存独立对比日志 |
| `ALIOTH_H5_CLI_MODE=actions` | 普通点击、滚动、返回与业务范围内导航可由 CLI 执行；仍走既有后态采集及验证 |
| `ALIOTH_PLAYWRIGHT_CLI_WORKSPACE_ROOT` | 平台可写的任务目录根，默认 `/tmp/alioth-playwright-cli` |

特殊视觉目标、弹窗关闭和已有专项几何验证能力在动作发送前选择旧执行器。CLI 已发出动作而结果未知时，不会切换执行器重放。

## 2. 为什么需要锁版兼容补丁

官方 CLI 的 CDP 连接使用 `noDefaults`，没有继承另一个 Playwright 连接创建浏览器时的设备模拟参数。实测在同一页面截取截图后，DPR 可从 2 变为 1，影响后续坐标和取证。

只去掉截图 `clip` 不能解决完整问题：虽然页面 DPR 不再改变，截图实际像素尺寸仍与平台采集不一致。这条方案未采用。

上下文补丁修改锁版 Playwright Core 的 CDP 默认上下文初始化：从平台进程传入已经验证的 viewport、screen、DPR、mobile、touch 参数，使用现有 Playwright 初始化代码建立一致的连接状态。没有替换 CLI 截图为 Python 截图，也没有在截图后偷偷修改回设备参数。

点击绑定补丁同时核对节点和语义：平台预检固定真实节点的 backendNodeId，并保存 text、aria-label、role、type、href 的摘要。CLI 在同一节点上重新核对后才点击。节点替换、脱离页面或同节点变成支付/外链语义时均拒绝，不重新选择其他节点，不修改 DOM，也不改用 Python 重放点击。

原文件 SHA-256：`7aa0bf8b6b69d32065912e3d8f7e3c18c62de4d668f770a8e039811d3cf9c6a0`。

补丁标识：`alioth-cdp-context-inheritance-v1`、`alioth-cli-backend-node-click-v1`。

补丁后 SHA-256：`c59cd8cd2f8b26153b9691de09d9b11885c675d08ab83e6242cfee54d380f2b6`（2026-09-07，同节点语义核验版本）。

安装脚本检查完整摘要、接入位置唯一性和重复执行一致性；运行时连接前再次校验完整摘要。升级依赖时必须重新审核补丁和运行环境对比测试，不能只更新版本字符串。CLI 仍是官方锁定发行版加平台兼容补丁，不能称为未经修改的官方二进制。

诊断依据为锁定依赖源码和本地 CDP 实验。协议参数参考 [Chrome Page.captureScreenshot](https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-captureScreenshot)，官方工具入口见 [Microsoft Playwright CLI](https://github.com/microsoft/playwright-cli)。协议文档本身不证明该副作用，结论来自实际复现。

## 3. 隔离与证据边界

- 浏览器、Profile、CLI 配置、缓存、浏览器注册及 daemon 目录均由平台在每会话独立目录创建，目录不接受 Agent 路径参数。
- 仅连接平台创建的本机回环 CDP 地址，不传递数据库、模型或代理凭证到 CLI 环境。
- 不加载用户全局 CLI 配置，不允许任意 Shell、CLI 子命令或 JavaScript。页面结构中的命令文字只是待分析数据。
- 每次只读对比前后核验 URL、当前标签以及宽高、DPR、屏幕、触摸、UA、方向和缩放。环境漂移时隔离会话，不能改走旧执行器继续操作。
- 对比截图有独立观察时序；以 `comparison_only` 记录，不改写正式动作列表，也不替代原 Capture 的截图或最后一次动作。
- CLI 命令回执的 `command_succeeded` 不等于 `arrival_verified`。正式页面到达仍由统一动作后的采集与后置验证决定。
- 正常关闭先 detach，再关闭所属浏览器并清理目录。detach 响应失败时仍关闭所属浏览器，不批量停止其他任务进程。
- API 与 Worker 容器必须启用 init 作为 PID1 子进程回收器：Compose 服务使用 `init: true`，直接 `docker run` 必须传 `--init`（例如 `docker run --init ...`）。CLI daemon 是容器内的子进程，不能把未启用 init 的 Docker 运行方式当作正式部署；应用关闭逻辑不能替代 PID1 回收。

## 4. 回归命令与证据

```bash
ALIOTH_CLI_INTEGRATION=1 .venv/bin/pytest services/api/tests/test_playwright_cli_runtime.py services/api/tests/test_web_capture_runtime_holder.py -q --disable-warnings
```

本轮主审记录为 58 项通过（52.84 秒）；其中实际 Chromium 样本覆盖手机 390×844/DPR=2、手机 320×640/DPR=3、桌面 1280×800/DPR=1，并验证静态同页 CLI 与 Python 截图尺寸及逐像素一致。包括只读拒绝点击、普通点击/返回/滚动、付款目标拒绝、节点替换拒绝、广告落地旧链路保留、环境漂移隔离、超限输出和关闭失败清理。

2026-09-07 主审重新运行 CLI、Runtime Holder、Browser Command Scope、H5 Secret Input Coordination 四个文件，77 项通过（53.19 秒）。包含未知动作后只读观察、外层取消留证及共享协调器边界；它们分别验证下层真实浏览器与上层执行合同，尚不等于完整业务任务验收。

同日生产 Dockerfile 构建隔离镜像 `alioth-api:exploration-20260907`，主审 `docker image inspect` 返回的 ID 为 `sha256:72f00f7c9e54ca1435051f3f67a25ca686ce1456fc2f1c964b2265677a8c4215`。在 Linux/ARM64、用户 10001、只读根目录、无外网、仅 /tmp 与 /run 可写条件下，CLI 测试文件 56 项通过。镜像和主机锁文件、补丁与 bundle 摘要一致；没有连接业务服务。

2026-09-07 dev_2.0 合并后当前源码挂载验收（独立于上述历史快照）：将当前源码以只读 `/workspace` 挂载到隔离 CLI 测试容器，使用基底镜像 `alioth-api:exploration-20260907`（ID 同上），不连接业务 DB/Redis，使用本机 `127.0.0.1` 测试站点。首次不带 `--init` 运行时为 55 项通过、5 项关闭回收断言失败；失败均表现为 CLI daemon 在浏览器关闭后仍被 Docker 默认 PID1 留存。随后以 `docker run --init` 重跑，结果为 60 项通过、2 个 warning，耗时 73.94 秒，退出码 0。该结果将 `--init` 确认为 API/Worker 的部署要求，而不是可忽略的测试条件。

同一隔离镜像中的 CLI 补丁摘要校验通过，`node --test infra/playwright-cli/patch-cdp-context.test.mjs` 为 3 项通过；本次仍未连接业务服务或完成生产业务任务验收。

Python Playwright 当前仍采用范围依赖，本次镜像实际解析为 1.62.0，与 CLI 的 Node Playwright 版本不同；当前样本通过不代表今后的任意依赖组合都兼容。后续依赖升级需重新执行上述验收。

最终独立暂存导出 `/tmp/alioth-bc-index-s6y12M` 重新干净安装 CLI 并校验补丁，Node 补丁测试 3 项通过；在 macOS / Node 24.18.0 上显式启用真实 CLI，CLI/Runtime Holder 60 项通过（54.76 秒）。共享执行与生产组合根另在跨批 56 文件中验证：1515 项通过、15 项条件跳过。旧采集扩大回归另有 7 项已在未接入 CLI 的基线原样复现的失败，完整边界见 [CLI 交付审查](../reviews/2026-09-07-cli-execution-review.md)。

以上不等于真实业务任务或 Completion Gate 验收。默认继续关闭 CLI；正式启用时先使用 observe 对照，满足实际产品验收后再启用普通操作。
