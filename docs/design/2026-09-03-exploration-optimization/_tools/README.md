# 文档维护与离线技术图

此目录只服务于七项设计文档，不参与业务构建，也不会连接设备、调用模型或操作质检任务。

## 内容与产物

上一级八份 Markdown 是内容源；七篇专项各有组件、时序、判断流程三段 Mermaid。专项二在第 5.10 节额外增加概念数据关系图，因此该篇依次为数据关系、组件、时序、判断四张，全系列共 22 张。标题和图数由 `render.mjs` 的文档配置约束，避免新增图后旧图标题错位。HTML 内嵌生成的布局、Canvas 核心和阅读脚本，读者无需安装依赖或访问 CDN。

- `render.mjs`：将 Markdown 转成自包含 HTML，并输出可审查的补丁。
- `diagram-build.mjs`：校验约定图源并预先计算布局。
- `diagram-canvas.js`：无 DOM 依赖的绘图及命中逻辑，可使用原生 Canvas 验证。
- `diagram-interface.js`：缩放、全图概览、节点/消息定位、文字详情和打印状态。

图表示设计职责，不表示相应业务模块已经实现。Canvas 失败或 JavaScript 被禁用时，原图源和文字关系仍可阅读。

数据关系图使用同样的纵向布局，但图例与节点详情表达包含/引用、可选关联和数量约束，不使用调用先后的解释；它不是物理数据库表图。

## 构建依赖

需要 Node.js、`marked`、`@dagrejs/dagre` 和 `@napi-rs/canvas`。本次布局核验使用 Dagre 3.1.1；Dagre 仅负责构建时布局，职责见 [官方说明](https://github.com/dagrejs/dagre/wiki)。依赖应放在独立文档工具环境，不修改业务项目依赖。

通过 `ALIOTH_DOC_MODULE_PATHS` 指定已有 `node_modules` 目录；多个目录使用当前系统的路径分隔符（macOS/Linux 为冒号）。也可从本工具可访问的常规 Node 依赖路径解析。没有所需依赖会明确失败，不下载运行时脚本。

构建时字体会影响换行和布局；正式更新应使用稳定的中文字体环境，并重新核对图形边界。读者字体不同不改变文档源内容；完整长标签也可通过节点详情与图源读取。

## 更新与验证

先修改 Markdown，再对指定文档生成补丁：

```sh
node docs/design/2026-09-03-exploration-optimization/_tools/render.mjs \
  docs/design/2026-09-03-exploration-optimization \
  01-controlled-exploration.md --update
```

命令只向标准输出写 `apply_patch` 格式的补丁，不直接覆写文件。审查后用 `apply_patch` 应用；同样方式处理其余六篇，索引源文件为 `README.md`，输出为 `index.html`。

验证生成内容是否精确同步：

```sh
node docs/design/2026-09-03-exploration-optimization/_tools/render.mjs \
  docs/design/2026-09-03-exploration-optimization \
  01-controlled-exploration.md --check
```

检查输出包含源 SHA256、章节数、图数和一致性结果。除同步检查外，还需验证 HTML5、链接/锚点、内嵌脚本、图形文本边界、节点命中与缩放；原生 Canvas 和最小 DOM 验证不等于真实浏览器整页布局验收，更不等于设备或 Completion Gate 验收。

## 约定图源语法

流程图只使用 `flowchart TB`；每行一个 `ID["标题<br/>说明"]` 或 `ID{"条件？"}`，连线使用 `A -->|交接说明| B` 或 `A -.->|回流说明| B`。

时序图使用 `sequenceDiagram`、`participant A as 参与者`、`A->>B:请求`、`B-->>A:回执`、`Note over A,B:说明`；允许 `autonumber`。默认不超过 6 个参与者、18 条消息；流程图默认不超过 12 个节点，以保持可读性。

本工具不是通用 Mermaid 渲染器；不支持的语法（例如嵌套 subgraph、alt/loop 或自定义样式）会直接报错，避免丢失设计分支。需要扩展时同步修改编译器、绘图器及其验证，不只让语法解析通过。
