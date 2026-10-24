# infrastructure/browser Playwright 浏览器驱动

封装 Playwright 页面自动化的工程细节（稳定等待、重试、截图、DOM/文本提取、控制台日志采集、网络请求采集、单次触发）。

## 职责

- 供 [agents/tools](../../agents/tools/README.md) 的 `page_capture_tool`、"页面识别 Tool"调用，Agent 只需表达"想做什么采集"，Playwright API、稳定等待、截图编码等工程细节内置于本层。
- `page_capture_tool` 是 Agent 可见入口；本目录是独立浏览器处理层，禁止把 Playwright API 直接暴露给 Agent。
- OCR 能力对接（PaddleOCR / 云 OCR，选型待定），供图片文字提取类规则使用。
- 截图比对能力（视觉差异检测），供视觉类规则使用。

## 关联

- 是 `agents/tools` 中 `page_capture_tool`、"页面识别/OCR/截图比对"等 Tool 的具体实现来源。
- 页面采集层生成完整内存产物，随后由应用层写入 [../storage/](../storage/README.md) 和 `qc_evidence_artifact`；Tool 只向 Agent 返回摘要与真实 `artifact_id`，禁止把完整 DOM 或截图 Base64 放入 LLM 上下文。
