# 产品脚本路由说明

实现：`app_product_orchestration_policy.try_product_script_override`。

- **默认**：当前包有专属编排且 Profile 不是 `agent_profile_only` → 走 `*_script_override`
- **强制 Agent**：`ALIOTH_APP_EXECUTION_MODE=agent` 或 Profile=`agent_profile_only`
- **强制编排对照**：`ALIOTH_APP_EXECUTION_MODE=legacy_script`

无专属编排时 Agent 合同：开屏语义相对 / 弹窗 Watcher cascade（D=「<」×2）/ 底栏语义相对。
