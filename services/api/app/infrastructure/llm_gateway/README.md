# infrastructure/llm_gateway LLM 网关

LLM / VLM 调用的统一抽象层，屏蔽底层供应商差异（OpenAI / x.ai）。

## 职责

- 统一的调用接口（Chat/Completion/结构化输出 Function Calling），供 [agents/tools](../../agents/tools/README.md) 的 AI 判断类 Tool 调用。
- 强制 JSON Schema 结构化输出、Prompt 版本管理。
- 成本与超时控制、限流与重试策略。
- 供应商切换只需修改本层实现，不影响 Agent 编排逻辑。

## 关联

- 被 `agents/tools` 中的"AI 判断 Tool"调用。
- 被 [agents/memory](../../agents/memory/README.md)（V4 情景/语义记忆的向量化处理，视具体实现可能复用 Embedding 能力）间接依赖。

## V1 实现（2026-07，Agent Framework 1.10.0）

`client_factory.py` 提供 `build_chat_client(settings: LLMSettings, *, usage_source: str, usage_operation: str | None = None, usage_scope_type: str | None = None) -> SupportsChatGetResponse`，根据配置屏蔽供应商差异，并要求调用方显式声明成本来源：

- **provider**：`openai`（官方 API）/ `azure_openai`（必须配置 `azure_endpoint`+`azure_api_version`）/ `xai`、`custom`（必须配置 `base_url`，走 OpenAI 兼容协议）。缺失必填项统一抛出 `LLMConfigurationError`（转换为结构化 JSON 错误响应，不会把供应商 SDK 的原始异常裸传给调用方）。
- **api_style**：`responses`（`OpenAIChatClient`，默认）/ `chat_completion`（`OpenAIChatCompletionClient`，用于尚不支持 Responses API 的第三方供应商）。
- **超时/重试**：`OpenAIChatClient`/`OpenAIChatCompletionClient` 构造函数本身不接受 `timeout`/`max_retries`，因此自行构造标准 `openai` SDK 的 `AsyncOpenAI`/`AsyncAzureOpenAI`（原生支持这两个参数）并通过 `async_client=` 注入。
- **配置来源**：`core/config.py` 的 `LLMSettings`，统一从 `ALIOTH_LLM_*` 环境变量读取（支持 `.env` 与 `.env.local`，优先级为"进程环境变量 > `.env.local` > `.env` > 代码默认值"），不依赖 `openai`/`OPENAI_API_KEY` 等包自身的环境变量兜底，避免鉴权/版本来源不透明。DeepSeek 等 OpenAI 兼容供应商建议使用 `provider=custom` + `api_style=chat_completion` + `base_url`。
- **成本来源**：`usage_source` 用于区分主 Agent、页面识别、Rule Judge、DevUI 调试等调用来源；`usage_scope_type` 用于区分 `task` 与 `debug`。即使调试入口不计入正式任务成本，也必须进入全量 LLM 调用流水，保证所有模型花费可审计。
- **直接 SDK 调用**：确需绕过 MAF `ChatClient` 直接调用 OpenAI-compatible SDK 时，必须在成功响应后调用 `record_current_openai_compatible_response_usage`，并显式传入 `source` 与 `operation`。新增直接 SDK 入口必须同步更新 `test_llm_usage_accounting_guard.py` 的 allowlist 与测试。

测试见 [../../../tests/test_llm_client_factory.py](../../../tests/test_llm_client_factory.py)。
